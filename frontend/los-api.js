(function initializeLosApi(root) {
    "use strict";

    async function fetchJson(url, options) {
        const response = await fetch(url, options);
        const raw = await response.text();
        let payload;

        try {
            payload = raw ? JSON.parse(raw) : {};
        }
        catch {
            const preview = raw.replace(/\s+/g, " ").slice(0, 300);
            throw new Error(`API returned HTTP ${response.status}: ${preview || "empty response"}`);
        }

        if (!response.ok) {
            throw new Error(payload.error || `API returned HTTP ${response.status}`);
        }

        return payload;
    }

    const api = { fetchJson };

    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }

    root.LosApi = api;
}(typeof globalThis !== "undefined" ? globalThis : this));
