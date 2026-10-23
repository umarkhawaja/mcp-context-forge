/**
 * Unit tests for teamSelect.js (edit-server / edit-gateway team selector).
 */

import { describe, test, expect, beforeEach, afterEach } from "vitest";

import { populateTeamSelect } from "../../../mcpgateway/admin_ui/teamSelect.js";

const options = (select) =>
  [...select.options].map((o) => ({
    value: o.value,
    text: o.textContent,
    selected: o.selected,
  }));

beforeEach(() => {
  document.body.innerHTML =
    '<select id="edit-server-team" name="team_id"></select>';
  window.USER_TEAMS = [
    { id: "eng", name: "engineering", role: "owner", is_personal: false },
    { id: "sup", name: "support", role: "owner", is_personal: false },
    { id: "docs", name: "MCP.Docs", role: "member", is_personal: false },
    { id: "me", name: "Umar's Team", role: "owner", is_personal: true },
  ];
});

afterEach(() => {
  delete window.USER_TEAMS;
  document.body.innerHTML = "";
});

describe("populateTeamSelect", () => {
  test("lists only owned teams and preselects the current team", () => {
    const select = populateTeamSelect("edit-server-team", {
      teamId: "sup",
      team: "support",
    });

    expect(options(select)).toEqual([
      { value: "eng", text: "engineering", selected: false },
      { value: "sup", text: "support (current)", selected: true },
      { value: "me", text: "Umar's Team (personal)", selected: false },
    ]);
    expect(select.disabled).toBe(false);
  });

  test("excludes teams where the user is only a member", () => {
    const select = populateTeamSelect("edit-server-team", {
      teamId: "eng",
      team: "engineering",
    });

    expect(options(select).map((o) => o.value)).not.toContain("docs");
  });

  test("keeps a current team the user does not own, so saving never moves the item", () => {
    const select = populateTeamSelect("edit-server-team", {
      teamId: "docs",
      team: "MCP.Docs",
    });

    expect(options(select)[0]).toEqual({
      value: "docs",
      text: "MCP.Docs (current)",
      selected: true,
    });
    expect(select.value).toBe("docs");
  });

  test("disables the select when there is nothing else to choose", () => {
    window.USER_TEAMS = [{ id: "eng", name: "engineering", role: "owner" }];
    const select = populateTeamSelect("edit-server-team", {
      teamId: "eng",
      team: "engineering",
    });

    expect(select.disabled).toBe(true);
  });

  test("handles missing USER_TEAMS and a missing element", () => {
    delete window.USER_TEAMS;
    const select = populateTeamSelect("edit-server-team", {
      teamId: "x",
      team: "x",
    });

    expect(options(select)).toEqual([
      { value: "x", text: "x (current)", selected: true },
    ]);
    expect(populateTeamSelect("no-such-select", {})).toBeNull();
  });

  test("renders team names as text, not HTML", () => {
    window.USER_TEAMS = [
      { id: "evil", name: "<img src=x onerror=alert(1)>", role: "owner" },
    ];
    const select = populateTeamSelect("edit-server-team", { teamId: "evil" });

    expect(select.querySelector("img")).toBeNull();
    expect(select.options[0].textContent).toContain("<img");
  });
});
