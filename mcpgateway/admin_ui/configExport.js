// ===============================================
// CONFIG EXPORT FUNCTIONALITY
// ===============================================

import { closeModal, openModal } from "./modals.js";
import {
  fetchWithTimeout,
  handleFetchError,
  safeGetElement,
  showErrorMessage,
  showSuccessMessage,
} from "./utils.js";

/**
 * Global variables to store current config data
 */
let currentConfigData = null;
let currentConfigType = null;
let currentServerName = null;
let currentServerId = null;

/**
 * Show the config selection modal
 * @param {string} serverId - The server UUID
 * @param {string} serverName - The server name
 */
export const showConfigSelectionModal = function (serverId, serverName) {
  currentServerId = serverId;
  currentServerName = serverName;

  const serverNameDisplay = safeGetElement("server-name-display");
  if (serverNameDisplay) {
    serverNameDisplay.textContent = serverName;
  }

  openModal("config-selection-modal");
};
/**
 * Build MCP_SERVER_CATALOG_URL for a given server
 * @param {Object} server
 * @returns {string}
 */
export const getCatalogUrl = function (server) {
  const currentHost = window.location.hostname;
  const currentPort =
    window.location.port ||
    (window.location.protocol === "https:" ? "443" : "80");
  const protocol = window.location.protocol;

  const baseUrl = `${protocol}//${currentHost}${
    currentPort !== "80" && currentPort !== "443" ? ":" + currentPort : ""
  }`;

  return `${baseUrl}/servers/${server.id}`;
};

/**
 * Generate and show configuration for selected type
 * @param {string} configType - Configuration type: 'sse' or 'http'
 */
export const generateAndShowConfig = async function (configType) {
  try {
    console.log(
      `Generating ${configType} config for server ${currentServerId}`
    );

    // First, fetch the server details
    const response = await fetchWithTimeout(
      `${window.ROOT_PATH}/admin/servers/${currentServerId}`
    );

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const server = await response.json();

    // Generate the configuration
    const config = generateConfig(server, configType);

    // Store data for modal
    currentConfigData = config;
    currentConfigType = configType;

    // Close selection modal and show config display modal
    closeModal("config-selection-modal");
    showConfigDisplayModal(server, configType, config);

    console.log("✓ Config generated successfully");
  } catch (error) {
    console.error("Error generating config:", error);
    const errorMessage = handleFetchError(error, "generate configuration");
    showErrorMessage(errorMessage);
  }
};

/**
 * Export server configuration in specified format
 * @param {string} serverId - The server UUID
 * @param {string} configType - Configuration type: 'sse' or 'http'
 */
export const exportServerConfig = async function (serverId, configType) {
  try {
    console.log(`Exporting ${configType} config for server ${serverId}`);

    // First, fetch the server details
    const response = await fetchWithTimeout(
      `${window.ROOT_PATH}/admin/servers/${serverId}`
    );

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const server = await response.json();

    // Generate the configuration
    const config = generateConfig(server, configType);

    // Store data for modal
    currentConfigData = config;
    currentConfigType = configType;
    currentServerName = server.name;

    // Show the modal with the config
    showConfigDisplayModal(server, configType, config);

    console.log("✓ Config generated successfully");
  } catch (error) {
    console.error("Error generating config:", error);
    const errorMessage = handleFetchError(error, "generate configuration");
    showErrorMessage(errorMessage);
  }
};

/**
 * Build an MCP client OAuth block for an OAuth-enabled server.
 *
 * Returns null when the server has no OAuth, no authorization server, or the
 * gateway has no public client ID configured (MCP_CLIENT_OAUTH_CLIENT_ID):
 * callers then fall back to a static bearer header.
 * @param {Object} server - Server object from API (camelCase fields)
 * @param {string} serverUrl - Public URL of the server's MCP endpoint
 * @returns {Object|null} - OAuth block, or null
 */
export const buildOAuthBlock = function (server, serverUrl) {
  const oauthConfig = server.oauthConfig || {};
  const issuer = (oauthConfig.authorization_servers || [])[0];
  const clientSettings = window.MCP_CLIENT_OAUTH || {};
  if (!server.oauthEnabled || !issuer || !clientSettings.client_id) {
    return null;
  }

  const scopeTemplate =
    clientSettings.scope_template ||
    "{server_url}/access_as_user offline_access";
  return {
    clientId: clientSettings.client_id,
    callbackPort: clientSettings.callback_port || 33418,
    // Entra serves metadata only at the OIDC path; the RFC 8414 path returns 404.
    authServerMetadataUrl: `${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`,
    scopes: scopeTemplate.split("{server_url}").join(serverUrl),
  };
};

/**
 * Generate configuration object based on server and type
 * @param {Object} server - Server object from API
 * @param {string} configType - Configuration type
 * @returns {Object} - Generated configuration object
 */
export const generateConfig = function (server, configType) {
  const currentHost = window.location.hostname;
  const currentPort =
    window.location.port ||
    (window.location.protocol === "https:" ? "443" : "80");
  const protocol = window.location.protocol;
  const baseUrl = `${protocol}//${currentHost}${currentPort !== "80" && currentPort !== "443" ? ":" + currentPort : ""}`;

  // Clean server name for use as config key (alphanumeric and hyphens only)
  const cleanServerName = server.name
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");

  switch (configType) {
    case "sse":
      return {
        servers: {
          [cleanServerName]: {
            type: "sse",
            url: `${baseUrl}/servers/${server.id}/sse`,
            headers: {
              Authorization: "Bearer your-token-here",
            },
          },
        },
      };

    case "http": {
      const oauth = buildOAuthBlock(
        server,
        `${baseUrl}/servers/${server.id}/mcp`
      );
      return {
        servers: {
          [cleanServerName]: {
            type: "streamable-http",
            url: `${baseUrl}/servers/${server.id}/mcp/`,
            ...(oauth
              ? { oauth }
              : { headers: { Authorization: "Bearer your-token-here" } }),
          },
        },
      };
    }

    case "claude": {
      // No trailing slash: the URL must equal the Entra identifier URI the scope is built from.
      const serverUrl = `${baseUrl}/servers/${server.id}/mcp`;
      const oauth = buildOAuthBlock(server, serverUrl);
      return {
        mcpServers: {
          [cleanServerName]: {
            type: "http",
            url: serverUrl,
            ...(oauth
              ? { oauth }
              : { headers: { Authorization: "Bearer your-token-here" } }),
          },
        },
      };
    }

    default:
      throw new Error(`Unknown config type: ${configType}`);
  }
};

/**
 * Show the config display modal with generated configuration
 * @param {Object} server - Server object
 * @param {string} configType - Configuration type
 * @param {Object} config - Generated configuration
 */
export const showConfigDisplayModal = function (server, configType, config) {
  const descriptions = {
    sse: "Configuration for LangChain, LlamaIndex, and other SSE-based frameworks",
    http: "Configuration for REST clients and HTTP-based MCP integrations",
    claude: "Claude Code .mcp.json configuration",
  };

  const usageInstructions = {
    sse: "Use with MCP client libraries that support Server-Sent Events transport",
    http: "Use with HTTP clients or REST API wrappers for MCP protocol",
    claude:
      "Commit as .mcp.json, or run `claude mcp add-json`. Claude Code signs in with OAuth on first use.",
  };

  const serverEntry =
    Object.values(config.mcpServers || config.servers || {})[0] || {};
  if (server.oauthEnabled && !serverEntry.oauth) {
    usageInstructions[configType] +=
      " This server has OAuth enabled, but MCP_CLIENT_OAUTH_CLIENT_ID is not set, so a bearer token placeholder is shown.";
  }

  // Update modal content
  const descriptionEl = safeGetElement("config-description");
  const usageEl = safeGetElement("config-usage");
  const contentEl = safeGetElement("config-content");

  if (descriptionEl) {
    descriptionEl.textContent = `${descriptions[configType]} for server "${server.name}"`;
  }

  if (usageEl) {
    usageEl.textContent = usageInstructions[configType];
  }

  if (contentEl) {
    contentEl.value = JSON.stringify(config, null, 2);
  }

  // Update title and open the modal
  const titleEl = safeGetElement("config-display-title");
  if (titleEl) {
    titleEl.textContent = `${configType.toUpperCase()} Configuration for ${server.name}`;
  }
  openModal("config-display-modal");
};

/**
 * Copy configuration to clipboard
 */
export const copyConfigToClipboard = async function () {
  const contentEl = safeGetElement("config-content");
  if (!contentEl) {
    showErrorMessage("Config content not found");
    return;
  }

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(contentEl.value);
      showSuccessMessage("Configuration copied to clipboard!");
      return;
    } catch (error) {
      console.error(
        "Clipboard API failed, trying execCommand fallback:",
        error
      );
    }
  }

  // execCommand fallback (deprecated but widely supported)
  try {
    contentEl.select();
    contentEl.setSelectionRange(0, 99999);
    if (document.execCommand("copy")) {
      showSuccessMessage("Configuration copied to clipboard!");
      return;
    }
  } catch (error) {
    console.error("execCommand fallback failed:", error);
  }

  showErrorMessage("Please copy the selected text manually (Ctrl+C)");
};

/**
 * Download configuration as JSON file
 */
export const downloadConfig = function () {
  if (!currentConfigData || !currentConfigType || !currentServerName) {
    showErrorMessage("No configuration data available");
    return;
  }

  try {
    const content = JSON.stringify(currentConfigData, null, 2);
    const blob = new Blob([content], { type: "application/json" });
    const url = window.URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = `${currentServerName}-${currentConfigType}-config.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);

    showSuccessMessage(`Configuration downloaded as ${a.download}`);
  } catch (error) {
    console.error("Error downloading config:", error);
    showErrorMessage("Failed to download configuration");
  }
};

/**
 * Go back to config selection modal
 */
export const goBackToSelection = function () {
  closeModal("config-display-modal");
  openModal("config-selection-modal");
};
