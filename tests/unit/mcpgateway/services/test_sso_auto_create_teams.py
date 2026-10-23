# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_sso_auto_create_teams.py
Copyright contributors to the MCP-CONTEXT-FORGE project
SPDX-License-Identifier: Apache-2.0

Tests for SSO_AUTO_CREATE_TEAMS: teams created from IdP claim values on sign-in.

These tests use a real in-memory SQLite database so the partial unique index,
the membership reconciliation, and the "creator is not a member" rule are
exercised end to end rather than through mocks.
"""

# Future
from __future__ import annotations

# Standard
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Third-Party
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import Base, EmailTeam, EmailTeamMember, EmailUser
from mcpgateway.services.sso_service import SSOService
from mcpgateway.services.team_management_service import TeamManagementService

ADMIN = "platform-admin@test.local"
ALICE = "alice@test.local"
BOB = "bob@test.local"
PREFIX = "CF-Team-"


@pytest.fixture
def db():
    """Yield a fresh in-memory database with three users."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    for email in (ADMIN, ALICE, BOB):
        session.add(EmailUser(email=email, password_hash="$argon2id$v=19$m=65536,t=3,p=1$test", full_name=email, is_admin=email == ADMIN, is_active=True))  # pragma: allowlist secret
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def auto_teams_on():
    """Enable SSO_AUTO_CREATE_TEAMS with the CF-Team- prefix."""
    with (
        patch.object(settings, "sso_auto_create_teams", True),
        patch.object(settings, "sso_auto_team_group_prefixes", [PREFIX]),
        patch.object(settings, "sso_auto_team_owner_suffix", "-Owners"),
        patch.object(settings, "sso_auto_team_visibility", "private"),
        patch.object(settings, "platform_admin_email", ADMIN),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_side_caches():
    """Stub cache and RBAC side effects that need Redis or the full role table."""
    with (
        patch("mcpgateway.services.team_management_service.admin_stats_cache.invalidate_teams", new=AsyncMock()),
        patch.object(TeamManagementService, "_assign_team_rbac_role", new=AsyncMock()),
        patch.object(TeamManagementService, "_revoke_team_rbac_role", new=AsyncMock(), create=True),
        patch.object(TeamManagementService, "invalidate_team_member_count_cache", new=AsyncMock()),
    ):
        yield


def _service(db) -> SSOService:
    with patch("mcpgateway.services.sso_service.get_encryption_service"):
        return SSOService(db)


def _provider(team_mapping=None):
    return SimpleNamespace(id="entra", team_mapping=team_mapping or {}, provider_metadata={})


def _memberships(db, email):
    rows = db.execute(select(EmailTeamMember, EmailTeam).join(EmailTeam, EmailTeam.id == EmailTeamMember.team_id).where(EmailTeamMember.user_email == email, EmailTeamMember.is_active.is_(True))).all()
    return {team.name: (member.role, member.grant_source) for member, team in rows}


def _sso_teams(db):
    return db.execute(select(EmailTeam).where(EmailTeam.source == "sso")).scalars().all()


# ---------------------------------------------------------------------------
# Claim value -> (team key, role)
# ---------------------------------------------------------------------------


class TestAutoTeamTarget:
    """Pure prefix/suffix resolution."""

    @pytest.mark.parametrize(
        ("claim", "expected"),
        [
            ("CF-Team-Eng", ("CF-Team-Eng", "member")),
            ("cf-team-eng", ("cf-team-eng", "member")),
            ("CF-Team-Eng-Owners", ("CF-Team-Eng", "owner")),
            ("CF-Team-Eng-owners", ("CF-Team-Eng", "owner")),
            ("Other-Group", None),
            ("CF-Team-", None),  # prefix alone is not a team
            ("CF-Team--Owners", None),  # owner suffix with an empty team name
            ("7ae029c8-d426-4a9f-a223-0312ccc33bf9", None),  # Entra object IDs never match a name prefix
        ],
    )
    def test_resolution(self, auto_teams_on, claim, expected):
        assert SSOService._auto_team_target(claim) == expected

    def test_empty_owner_suffix_makes_everything_member(self, auto_teams_on):
        with patch.object(settings, "sso_auto_team_owner_suffix", ""):
            assert SSOService._auto_team_target("CF-Team-Eng-Owners") == ("CF-Team-Eng-Owners", "member")


# ---------------------------------------------------------------------------
# Sign-in reconciliation
# ---------------------------------------------------------------------------


class TestAutoCreateOnSignIn:
    """End-to-end behaviour of _apply_team_mapping with auto-creation enabled."""

    @pytest.mark.asyncio
    async def test_creates_team_and_adds_member(self, db, auto_teams_on):
        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())

        teams = _sso_teams(db)
        assert [(t.name, t.external_provider, t.external_id, t.visibility) for t in teams] == [("CF-Team-Eng", "entra", "cf-team-eng", "private")]
        assert _memberships(db, ALICE) == {"CF-Team-Eng": ("member", "sso")}

    @pytest.mark.asyncio
    async def test_creator_is_not_a_member(self, db, auto_teams_on):
        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())

        team = _sso_teams(db)[0]
        assert team.created_by == ADMIN
        assert "CF-Team-Eng" not in _memberships(db, ADMIN)

    @pytest.mark.asyncio
    async def test_second_sign_in_reuses_team(self, db, auto_teams_on):
        service = _service(db)
        await service._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())
        await service._apply_team_mapping(BOB, {"groups": ["cf-team-eng"]}, _provider())

        assert len(_sso_teams(db)) == 1
        assert _memberships(db, BOB) == {"CF-Team-Eng": ("member", "sso")}

    @pytest.mark.asyncio
    async def test_owner_claim_grants_owner(self, db, auto_teams_on):
        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng-Owners"]}, _provider())

        assert len(_sso_teams(db)) == 1
        assert _memberships(db, ALICE) == {"CF-Team-Eng": ("owner", "sso")}

    @pytest.mark.asyncio
    async def test_member_and_owner_claims_resolve_to_owner(self, db, auto_teams_on):
        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng", "CF-Team-Eng-Owners"]}, _provider())

        assert len(_sso_teams(db)) == 1
        assert _memberships(db, ALICE) == {"CF-Team-Eng": ("owner", "sso")}

    @pytest.mark.asyncio
    async def test_removed_claim_revokes_membership(self, db, auto_teams_on):
        service = _service(db)
        await service._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())
        await service._apply_team_mapping(ALICE, {"groups": []}, _provider())

        assert _memberships(db, ALICE) == {}
        assert len(_sso_teams(db)) == 1, "the team itself is kept so gateways and tools it owns are not stranded"

    @pytest.mark.asyncio
    async def test_explicit_mapping_wins_over_auto(self, db, auto_teams_on):
        manual = EmailTeam(name="engineering", slug="engineering", created_by=ADMIN, is_personal=False, visibility="private", is_active=True)
        db.add(manual)
        db.commit()

        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider({"CF-Team-Eng": {"team_id": manual.id, "role": "member"}}))

        assert _sso_teams(db) == []
        assert _memberships(db, ALICE) == {"engineering": ("member", "sso")}

    @pytest.mark.asyncio
    async def test_explicit_and_auto_mappings_combine(self, db, auto_teams_on):
        manual = EmailTeam(name="support", slug="support", created_by=ADMIN, is_personal=False, visibility="private", is_active=True)
        db.add(manual)
        db.commit()
        guid = "63cbee10-e24e-4bcf-b06f-f36a8661b1fa"

        await _service(db)._apply_team_mapping(ALICE, {"groups": [guid, "CF-Team-Eng"]}, _provider({guid: manual.id}))

        assert _memberships(db, ALICE) == {"support": ("member", "sso"), "CF-Team-Eng": ("member", "sso")}

    @pytest.mark.asyncio
    async def test_allow_team_creation_false_does_not_block(self, db, auto_teams_on):
        with patch.object(settings, "allow_team_creation", False):
            await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())

        assert _memberships(db, ALICE) == {"CF-Team-Eng": ("member", "sso")}

    @pytest.mark.asyncio
    async def test_inactive_sso_team_is_reactivated(self, db, auto_teams_on):
        service = _service(db)
        await service._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())
        team = _sso_teams(db)[0]
        team.is_active = False
        db.commit()

        await service._apply_team_mapping(BOB, {"groups": ["CF-Team-Eng"]}, _provider())

        db.refresh(team)
        assert team.is_active is True
        assert len(_sso_teams(db)) == 1


# ---------------------------------------------------------------------------
# Deny paths: nothing is created or granted
# ---------------------------------------------------------------------------


class TestAutoCreateDenyPaths:
    """The feature must stay inert unless explicitly enabled and matched."""

    @pytest.mark.asyncio
    async def test_feature_disabled_creates_nothing(self, db):
        with patch.object(settings, "sso_auto_create_teams", False), patch.object(settings, "sso_auto_team_group_prefixes", [PREFIX]):
            await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())

        assert _sso_teams(db) == []
        assert _memberships(db, ALICE) == {}

    @pytest.mark.asyncio
    async def test_enabled_without_prefixes_creates_nothing(self, db):
        with patch.object(settings, "sso_auto_create_teams", True), patch.object(settings, "sso_auto_team_group_prefixes", []):
            await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng", "anything"]}, _provider())

        assert _sso_teams(db) == []

    @pytest.mark.asyncio
    async def test_non_matching_claims_are_ignored(self, db, auto_teams_on):
        await _service(db)._apply_team_mapping(ALICE, {"groups": ["Finance", "7ae029c8-d426-4a9f-a223-0312ccc33bf9"]}, _provider())

        assert _sso_teams(db) == []
        assert _memberships(db, ALICE) == {}

    @pytest.mark.asyncio
    async def test_no_provider_does_nothing(self, db, auto_teams_on):
        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, None)

        assert _sso_teams(db) == []

    @pytest.mark.asyncio
    async def test_name_collision_with_manual_team_never_binds_it(self, db, auto_teams_on):
        manual = EmailTeam(name="CF-Team-Eng", slug="cf-team-eng", created_by=ADMIN, is_personal=False, visibility="private", is_active=True)
        db.add(manual)
        db.commit()

        await _service(db)._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())

        db.refresh(manual)
        assert manual.source is None and manual.external_id is None
        assert _memberships(db, ALICE) == {}, "a claim must not grant access to an unrelated team that shares its name"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("claim", ["CF-Team-<script>", "CF-Team-Eng;DROP", "CF-Team-" + "x" * 300])
    async def test_invalid_team_name_claims_are_skipped(self, db, auto_teams_on, claim):
        await _service(db)._apply_team_mapping(ALICE, {"groups": [claim]}, _provider())

        assert _sso_teams(db) == []
        assert _memberships(db, ALICE) == {}

    @pytest.mark.asyncio
    async def test_other_provider_does_not_reuse_team(self, db, auto_teams_on):
        service = _service(db)
        await service._apply_team_mapping(ALICE, {"groups": ["CF-Team-Eng"]}, _provider())
        keycloak = SimpleNamespace(id="keycloak", team_mapping={}, provider_metadata={})

        await service._apply_team_mapping(BOB, {"groups": ["CF-Team-Eng"]}, keycloak)

        assert _memberships(db, BOB) == {}, "the same claim value from another IdP must not join that IdP's team"


# ---------------------------------------------------------------------------
# get_or_create_external_team
# ---------------------------------------------------------------------------


class TestGetOrCreateExternalTeam:
    """Service-level binding and race handling."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_visibility(self, db):
        with pytest.raises(ValueError):
            await TeamManagementService(db).get_or_create_external_team("entra", "cf-team-eng", "CF-Team-Eng", ADMIN, visibility="team")

    @pytest.mark.asyncio
    async def test_unique_index_blocks_duplicate_binding(self, db):
        service = TeamManagementService(db)
        await service.get_or_create_external_team("entra", "cf-team-eng", "CF-Team-Eng", ADMIN)

        db.add(EmailTeam(name="Dup", slug="dup", created_by=ADMIN, is_personal=False, source="sso", external_provider="entra", external_id="cf-team-eng"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    @pytest.mark.asyncio
    async def test_unbound_teams_do_not_conflict(self, db):
        db.add_all(
            [
                EmailTeam(name="A", slug="a", created_by=ADMIN, is_personal=False),
                EmailTeam(name="B", slug="b", created_by=ADMIN, is_personal=False),
            ]
        )
        db.commit()  # two NULL external_id rows must not trip the partial unique index

    @pytest.mark.asyncio
    async def test_concurrent_insert_returns_winner(self, db):
        service = TeamManagementService(db)
        winner = EmailTeam(name="CF-Team-Eng", slug="winner", created_by=ADMIN, is_personal=False, source="sso", external_provider="entra", external_id="cf-team-eng")
        original_query = db.query
        calls = {"n": 0}

        def query_hiding_winner_once(*args, **kwargs):
            # Simulate the race: the lookup misses, then another worker commits the same binding.
            calls["n"] += 1
            if calls["n"] == 1:
                db.add(winner)
                db.commit()
                return original_query(EmailTeam).filter(EmailTeam.id == "never")
            return original_query(*args, **kwargs)

        with patch.object(db, "query", side_effect=query_hiding_winner_once):
            team = await service.get_or_create_external_team("entra", "cf-team-eng", "CF-Team-Eng-Other", ADMIN)

        assert team is not None and team.id == winner.id
        assert len(_sso_teams(db)) == 1
