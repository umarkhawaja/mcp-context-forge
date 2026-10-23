/**
 * Team selector for the edit-server and edit-gateway forms.
 *
 * Lists the teams the signed-in user owns (window.USER_TEAMS, role "owner"),
 * because the backend only accepts a team move into a team the caller owns.
 * The item's current team is always listed and preselected, so saving the
 * form without touching the selector never moves the item.
 */

/**
 * Fill a team <select> for an item being edited.
 * @param {string} selectId - ID of the <select> element
 * @param {Object} item - Server or gateway from the API (camelCase fields)
 * @returns {HTMLSelectElement|null} - The populated select, or null when absent
 */
export const populateTeamSelect = function (selectId, item) {
  const select = document.getElementById(selectId);
  if (!select) {
    return null;
  }

  const currentTeamId = item?.teamId || "";
  const currentTeamName = item?.team || "";
  const teams = Array.isArray(window.USER_TEAMS) ? window.USER_TEAMS : [];
  const ownedTeams = teams.filter(
    (team) => team && team.id && team.role === "owner"
  );

  select.replaceChildren();

  const options = [];
  if (currentTeamId && !ownedTeams.some((team) => team.id === currentTeamId)) {
    options.push({
      id: currentTeamId,
      label: `${currentTeamName || currentTeamId} (current)`,
    });
  }
  for (const team of ownedTeams) {
    const suffix = team.is_personal ? " (personal)" : "";
    const current = team.id === currentTeamId ? " (current)" : "";
    options.push({ id: team.id, label: `${team.name}${suffix}${current}` });
  }

  for (const { id, label } of options) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    option.selected = id === currentTeamId;
    select.appendChild(option);
  }

  // Nothing to choose from: keep the field (so the value is still submitted) but disable it.
  select.disabled = options.length <= 1;
  return select;
};
