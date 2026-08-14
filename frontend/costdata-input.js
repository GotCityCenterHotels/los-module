(function () {
    "use strict";
    const API = "/api/costdata/settings";
    const PROPERTIES_API = "/api/costdata/properties";
    const SOURCES_API = "/api/costdata/sources";
    const hotel = document.getElementById("settingsHotel"), form = document.getElementById("settingsForm");
    const layout = document.getElementById("settingsLayout"), status = document.getElementById("settingsStatus");
    const errorPanel = document.getElementById("settingsError"), save = document.getElementById("saveSettings");
    const dirtyState = document.getElementById("dirtyState"), importButton = document.getElementById("runImportButton");
    let model = null, dirty = false, loadedEnterpriseId = "";

    // Whole-number fields (guest counts, arrival counts) stay integers; every
    // money/hours field is fixed at two decimals.
    const INTEGER_FIELDS = new Set([
        "minGuests", "maxGuests", "minArrivals", "maxArrivals"
    ]);
    const DECIMALS = 2;

    const configs = {
        arrivalTiers: [["minArrivals","Min arrivals","number"],["maxArrivals","Max arrivals","number"],["receptionHours","Reception hours","number"]],
        breakfastTiers: [["minGuests","Min guests","number"],["maxGuests","Max guests","number"],["staffHours","Staff hours","number"]]
    };
    const defaults = { arrivalTiers:{minArrivals:0,maxArrivals:"",receptionHours:0}, breakfastTiers:{minGuests:0,maxGuests:"",staffHours:0}, distributionGroups:{groupName:"",costPercent:0,rules:[]} };

    // Rates, channels and room categories for the selected hotel, from
    // /api/costdata/sources. Null until loaded; an empty object after a failed
    // load, so the page still works with manual entry.
    let sources = null;

    async function loadSources(enterpriseId) {
        try {
            const payload = await LosApi.fetchJson(
                `${SOURCES_API}/${encodeURIComponent(enterpriseId)}`
            );
            sources = payload.data || {};
        }
        catch (error) {
            sources = {rates: [], channels: [], cleaningCategories: [], error: error.message};
            console.warn("Source lookup failed; manual entry still available.", error);
        }
    }

    function optionsFor(matchType) {
        if (!sources) return [];
        return (matchType === "rate" ? sources.rates : sources.channels) || [];
    }
    // Which group each value is already assigned to, so the picker can show it
    // as taken rather than silently allowing a duplicate across groups.
    function assignmentIndex(matchType, exceptGroup, exceptRuleIndex) {
        const assigned = new Map();
        model.distributionGroups.forEach((group, groupIndex) => {
            group.rules.forEach((rule, ruleIndex) => {
                if (rule.matchType !== matchType) return;
                if (groupIndex === exceptGroup && ruleIndex === exceptRuleIndex) return;
                const value = String(rule.matchValue || "").trim();
                if (value) assigned.set(value.toLowerCase(), group.groupName || `Group ${groupIndex + 1}`);
            });
        });
        return assigned;
    }

    function toFixedDecimals(value) {
        if (value === "" || value === null || value === undefined) return "";
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed.toFixed(DECIMALS) : value;
    }
    // Applied on blur rather than on input, so typing "1.5" is not rewritten to
    // "1.50" mid-keystroke and the caret does not jump.
    function bindDecimalNormalisation(input, field, onChange) {
        if (input.type !== "number" || INTEGER_FIELDS.has(field)) return;
        input.step = "0.01";
        input.addEventListener("blur", () => {
            const normalised = toFixedDecimals(input.value);
            if (normalised !== input.value) {
                input.value = normalised;
                if (onChange) onChange(normalised);
            }
        });
    }

    async function loadHotels() {
        status.textContent = "Loading properties...";
        hotel.disabled = true;
        try {
            const payload = await LosApi.fetchJson(PROPERTIES_API, {cache: "no-store"});
            const properties = (payload.data || []).filter((property) =>
                property && property.enterpriseId != null && String(property.enterpriseId).trim()
                && property.hotelName && String(property.hotelName).trim()
            );
            hotel.replaceChildren(new Option("Select property", ""));
            for (const property of properties) {
                hotel.add(new Option(property.hotelName, String(property.enterpriseId)));
            }
            if (properties.length) {
                hotel.value = String(properties[0].enterpriseId);
                await loadSettings(hotel.value);
            }
            else {
                layout.hidden = true;
                status.textContent = "No properties were found in the source or imported cost data.";
                hotel.disabled = false;
            }
        }
        catch (error) { hotel.disabled = false; showError(error); }
    }
    async function loadSettings(name) {
        if (!name || name === "undefined" || name === "null") {
            layout.hidden = true;
            status.textContent = "Select a property to begin.";
            return;
        }
        setBusy(true); errorPanel.hidden = true;
        const selectedOption = hotel.options[hotel.selectedIndex];
        const parameters = new URLSearchParams({
            hotelName: selectedOption ? selectedOption.textContent : ""
        });
        try {
            const [payload] = await Promise.all([
                LosApi.fetchJson(`${API}/${encodeURIComponent(name)}?${parameters}`, {cache: "no-store"}),
                loadSources(name)
            ]);
            model = payload.data;
            loadedEnterpriseId = model.enterpriseId;
            render();
            layout.hidden = false;
            setDirty(false);
            status.textContent = sources && sources.error
                ? `Editing ${model.hotelName} - this hotel's rate and room lists could not be loaded`
                : `Editing ${model.hotelName}`;
        }
        catch (error) { showError(error); } finally { setBusy(false); }
    }
    function render() {
        for (const [key, value] of Object.entries(model.profile)) {
            const input = form.elements.namedItem(key);
            if (!input) continue;
            input.value = input.type === "number" ? toFixedDecimals(value) : value;
            bindDecimalNormalisation(input, key);
        }
        renderDistribution();
        renderCleaning();
        for (const key of Object.keys(configs)) renderRows(key);
    }

    // Cleaning rows are one per (room category, occupancy) and come from the
    // hotel's own room categories: occupancy runs 1..(capacity + extraCapacity).
    // Any saved row whose category no longer exists in the hotel is kept and
    // marked, rather than silently dropped along with its costs.
    function mergeCleaningWithHotel() {
        const saved = new Map(
            (model.cleaningCategories || []).map(row =>
                [`${String(row.categoryName).toLowerCase()}|${row.occupancy}`, row])
        );
        const merged = [];
        for (const category of (sources && sources.cleaningCategories) || []) {
            for (const occupancy of category.occupancies) {
                const key = `${category.categoryName.toLowerCase()}|${occupancy}`;
                const existing = saved.get(key);
                saved.delete(key);
                merged.push({
                    categoryName: category.categoryName,
                    resourceCategoryId: category.categoryId,
                    occupancy,
                    capacity: category.capacity,
                    extraCapacity: category.extraCapacity,
                    cleaningMinutes: existing ? existing.cleaningMinutes : 0,
                    linenCost: existing ? existing.linenCost : 0,
                    fromHotel: true
                });
            }
        }
        for (const orphan of saved.values()) merged.push({...orphan, fromHotel: false});
        model.cleaningCategories = merged;
        return merged;
    }

    function renderCleaning() {
        const root = document.getElementById("cleaningCategories");
        if (!root) return;
        const rows = mergeCleaningWithHotel();
        root.replaceChildren();

        let currentCategory = null;
        for (const [index, row] of rows.entries()) {
            if (row.categoryName !== currentCategory) {
                currentCategory = row.categoryName;
                const heading = document.createElement("h3");
                heading.className = "cleaning-group";
                heading.textContent = row.fromHotel
                    ? `${row.categoryName} - standard ${row.capacity}${row.extraCapacity ? ` + ${row.extraCapacity} extra` : ""}`
                    : `${row.categoryName} - no longer in this hotel`;
                root.append(heading);
            }

            const line = document.createElement("div");
            line.className = "rule-row cleaning-row";
            if (!row.fromHotel) line.classList.add("is-orphaned");

            const occupancy = document.createElement("label");
            occupancy.textContent = "Guests";
            const occupancyValue = document.createElement("input");
            occupancyValue.type = "number";
            occupancyValue.value = row.occupancy;
            occupancyValue.readOnly = true;
            occupancyValue.tabIndex = -1;
            occupancy.append(occupancyValue);
            line.append(occupancy);

            for (const [field, label] of [["cleaningMinutes", "Minutes"], ["linenCost", "Linen cost"]]) {
                const wrap = document.createElement("label");
                wrap.textContent = label;
                const input = document.createElement("input");
                input.type = "number";
                input.min = "0";
                input.step = "0.01";
                input.value = toFixedDecimals(row[field]);
                input.dataset.field = field;
                bindDecimalNormalisation(input, field, (value) => { row[field] = value; });
                wrap.append(input);
                line.append(wrap);
            }

            if (!row.fromHotel) {
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "remove-rule";
                remove.textContent = "Remove";
                remove.onclick = () => {
                    model.cleaningCategories.splice(index, 1);
                    renderCleaning();
                    setDirty(true);
                };
                line.append(remove);
            }

            bindFields(line, row);
            root.append(line);
        }

        if (!rows.length) {
            emptyMessage(root, sources && sources.error
                ? "Could not load this hotel's room categories."
                : "No room categories found for this hotel.");
        }
    }
    function renderDistribution() {
        const root=document.getElementById("distributionGroups"); root.replaceChildren();
        model.distributionGroups.forEach((group,index)=>{
            const row=document.createElement("div"); row.className="rule-row distribution-rule";
            row.innerHTML=`<div class="rule-main"><label>Group name<input data-field="groupName" value="${escapeHtml(group.groupName)}" required></label><label>Cost %<input data-field="costPercent" type="number" min="0" max="100" step="0.01" value="${escapeHtml(toFixedDecimals(group.costPercent))}" required></label><button type="button" class="remove-rule" aria-label="Remove group">Remove</button></div><div class="match-list"></div><button type="button" class="text-button add-match">+ Add rate or channel match</button>`;
            bindDecimalNormalisation(
                row.querySelector('[data-field="costPercent"]'),
                "costPercent",
                (value) => { group.costPercent = value; }
            );
            row.querySelector(".remove-rule").onclick=()=>removeRow("distributionGroups",index); row.querySelector(".add-match").onclick=()=>{group.rules.push({matchType:"channel",matchValue:""});renderDistribution();setDirty(true)};
            const matches = row.querySelector(".match-list");
            group.rules.forEach((rule, ruleIndex) => {
                matches.append(buildMatchRow(group, index, rule, ruleIndex));
            });
            bindFields(row,group); root.append(row);
        }); emptyMessage(root,"No distribution groups yet.");
    }
    // A datalist gives native type-to-search without a custom dropdown widget.
    // Values already used by another group are listed in a trailing "Already
    // assigned" section showing their group, and are rejected on selection -
    // the same value in two groups would make the cost percentage ambiguous.
    function buildMatchRow(group, groupIndex, rule, ruleIndex) {
        const match = document.createElement("div");
        match.className = "match-row";

        const type = document.createElement("select");
        type.setAttribute("aria-label", "Match type");
        type.add(new Option("Channel", "channel", false, rule.matchType === "channel"));
        type.add(new Option("Rate", "rate", false, rule.matchType === "rate"));

        const listId = `match-options-${groupIndex}-${ruleIndex}`;
        const value = document.createElement("input");
        value.setAttribute("aria-label", "Match value");
        value.setAttribute("list", listId);
        value.required = true;
        value.value = rule.matchValue || "";
        value.placeholder = "Search rates and channels...";

        const list = document.createElement("datalist");
        list.id = listId;

        const note = document.createElement("small");
        note.className = "match-note";

        function refreshOptions() {
            const available = optionsFor(rule.matchType);
            const assigned = assignmentIndex(rule.matchType, groupIndex, ruleIndex);
            list.replaceChildren();

            const free = available.filter(item => !assigned.has(item.name.toLowerCase()));
            const taken = available.filter(item => assigned.has(item.name.toLowerCase()));

            for (const item of free) list.append(new Option(item.name, item.name));
            // Native datalist cannot disable an option, so assigned values are
            // pushed to the bottom and labelled with their group instead.
            for (const item of taken) {
                const option = new Option(
                    `${item.name} - already in ${assigned.get(item.name.toLowerCase())}`,
                    item.name
                );
                option.dataset.assigned = "true";
                list.append(option);
            }

            if (!available.length) {
                note.textContent = sources && sources.error
                    ? "Could not load this hotel's list - type the value manually."
                    : "No values found for this hotel - type the value manually.";
            }
            else {
                note.textContent = `${free.length} available, ${taken.length} already assigned`;
            }
        }

        function applyValue(raw) {
            const entered = String(raw || "").trim();
            const assigned = assignmentIndex(rule.matchType, groupIndex, ruleIndex);
            const owner = assigned.get(entered.toLowerCase());
            if (owner) {
                value.setCustomValidity(`"${entered}" is already assigned to ${owner}.`);
                note.textContent = `Already assigned to ${owner}. Pick another value.`;
                match.classList.add("is-duplicate");
            }
            else {
                value.setCustomValidity("");
                match.classList.remove("is-duplicate");
                refreshOptions();
            }
            rule.matchValue = entered;
            setDirty(true);
        }

        type.onchange = () => {
            rule.matchType = type.value;
            rule.matchValue = "";
            value.value = "";
            value.setCustomValidity("");
            refreshOptions();
            setDirty(true);
        };
        value.oninput = () => applyValue(value.value);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove match");
        remove.textContent = "×";
        remove.onclick = () => {
            group.rules.splice(ruleIndex, 1);
            renderDistribution();
            setDirty(true);
        };

        refreshOptions();
        if (rule.matchValue) applyValue(rule.matchValue);
        match.append(type, value, list, remove, note);
        return match;
    }

    function renderRows(key) {
        const root = document.getElementById(key);
        if (!root) return;
        root.replaceChildren();
        model[key].forEach((item, index) => {
            const row = document.createElement("div");
            row.className = "rule-row";
            for (const [field, label, type] of configs[key]) {
                const wrap = document.createElement("label");
                wrap.textContent = label;
                const input = document.createElement("input");
                input.type = type;
                if (type === "number") {
                    input.min = "0";
                    input.step = INTEGER_FIELDS.has(field) ? "1" : "0.01";
                    input.value = INTEGER_FIELDS.has(field)
                        ? (item[field] ?? "")
                        : toFixedDecimals(item[field]);
                }
                else {
                    input.value = item[field] ?? "";
                }
                input.dataset.field = field;
                bindDecimalNormalisation(input, field, (value) => { item[field] = value; });
                wrap.append(input);
                row.append(wrap);
            }
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "remove-rule";
            remove.textContent = "Remove";
            remove.onclick = () => removeRow(key, index);
            row.append(remove);
            bindFields(row, item);
            root.append(row);
        });
        emptyMessage(root, "No rules added yet.");
    }
    function bindFields(root,item){root.querySelectorAll("[data-field]").forEach(input=>input.addEventListener("input",()=>{item[input.dataset.field]=input.type==="checkbox"?input.checked:input.value;setDirty(true)}))}
    function emptyMessage(root,text){if(!root.children.length){const p=document.createElement("p");p.className="rules-empty";p.textContent=text;root.append(p)}}
    function removeRow(key,index){model[key].splice(index,1);key==="distributionGroups"?renderDistribution():renderRows(key);setDirty(true)}
    function escapeHtml(value){return String(value??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;")}
    function collect(){for(const input of form.querySelectorAll("[name]"))model.profile[input.name]=input.value;return model}

    function sectionOf(element) {
        const section = element.closest("[data-settings-section]");
        return section ? section.dataset.settingsSection : null;
    }
    function showSection(name) {
        for (const button of document.querySelectorAll(".settings-nav button")) {
            if (button.dataset.section === name) button.setAttribute("aria-current", "page");
            else button.removeAttribute("aria-current");
        }
        for (const section of document.querySelectorAll("[data-settings-section]")) {
            section.hidden = section.dataset.settingsSection !== name;
        }
    }
    // Only one section is visible at a time, but every section's inputs stay in
    // the form. reportValidity() therefore fails on a control the browser cannot
    // scroll to or focus, returns false, and the submit aborts with no feedback
    // at all - which is why the save button appeared to do nothing. Reveal the
    // offending section first, then report.
    function revealFirstInvalidControl() {
        const invalid = form.querySelector(":invalid");
        if (!invalid) return true;
        const section = sectionOf(invalid);
        if (section) showSection(section);
        form.reportValidity();
        invalid.focus({preventScroll: false});
        const label = invalid.closest("label");
        const fieldName = (label ? label.textContent.trim() : invalid.name) || "A field";
        showError(new Error(
            `${fieldName} needs a valid value before these settings can be saved.`
        ));
        return false;
    }

    async function submit(event) {
        event.preventDefault();
        errorPanel.hidden = true;
        if (!revealFirstInvalidControl()) return;
        setBusy(true);
        try {
            const payload = await LosApi.fetchJson(
                `${API}/${encodeURIComponent(hotel.value)}`,
                {
                    method: "PUT",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(collect())
                }
            );
            model = payload.data;
            loadedEnterpriseId = model.enterpriseId;
            render();
            setDirty(false);
            status.textContent = `Saved ${model.hotelName}`;
        }
        catch (error) { showError(error); }
        finally { setBusy(false); }
    }
    function setDirty(value){dirty=value;dirtyState.textContent=value?"Unsaved changes":"No unsaved changes";dirtyState.classList.toggle("is-dirty",value)}
    function setBusy(value){save.disabled=value;hotel.disabled=value;document.querySelector(".settings-workspace").setAttribute("aria-busy",String(value))}
    function showError(error){errorPanel.textContent=error.message||"Unable to load cost settings.";errorPanel.hidden=false;status.textContent="Something went wrong."}
    const IMPORT_POLL_INTERVAL_MS = 2000;
    const IMPORT_POLL_TIMEOUT_MS = 35 * 60 * 1000;
    const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
    async function waitForImport(statusUrl) {
        const deadline = Date.now() + IMPORT_POLL_TIMEOUT_MS;
        while (Date.now() < deadline) {
            const payload = await LosApi.fetchJson(statusUrl, {cache: "no-store"});
            const job = payload.job || {};
            if (job.status === "succeeded") return job;
            if (job.status === "failed") throw new Error(job.error || "Import failed.");
            status.textContent = `Cost data import ${job.status || "queued"}${job.attemptCount ? ` (attempt ${job.attemptCount})` : ""}...`;
            await delay(IMPORT_POLL_INTERVAL_MS);
        }
        throw new Error("The import is still running. Reload this page to check its status.");
    }
    // costdata/import is a FUNCTION-level route: it triggers a full
    // cross-database import, and the Function App is reachable on its own public
    // hostname, so it cannot be left open. Static Web Apps does not attach the
    // key for us, so the operator supplies it once per browser session.
    const IMPORT_KEY_STORAGE = "costdata-import-key";
    function importKey() {
        let key = "";
        try { key = sessionStorage.getItem(IMPORT_KEY_STORAGE) || ""; } catch { key = ""; }
        if (!key) {
            key = (prompt(
                "Enter the Function App key for the cost data import.\n\n"
                + "Azure portal > los-functions > App Keys. Stored for this browser session only."
            ) || "").trim();
            if (!key) return "";
            try { sessionStorage.setItem(IMPORT_KEY_STORAGE, key); } catch { /* session-only */ }
        }
        return key;
    }
    function forgetImportKey() {
        try { sessionStorage.removeItem(IMPORT_KEY_STORAGE); } catch { /* nothing cached */ }
    }
    async function runImport(){
        if(!confirm("Import all cost datasets now? This can take a while.")) return;
        const key = importKey();
        if (!key) { status.textContent = "Import cancelled - no Function App key supplied."; return; }
        importButton.disabled=true; errorPanel.hidden=true;
        try {
            status.textContent = "Queueing cost data import...";
            let accepted;
            try {
                accepted = await LosApi.fetchJson("/api/costdata/import", {
                    method: "POST",
                    headers: {"Content-Type": "application/json", "x-functions-key": key},
                    body: JSON.stringify({dataset:"all"})
                });
            }
            catch (error) {
                // A rejected key must not stay cached, or every later attempt
                // fails without ever asking again.
                if (/\b401\b|\b403\b/.test(error.message || "")) {
                    forgetImportKey();
                    throw new Error("The Function App key was rejected. Click the button again to re-enter it.");
                }
                throw error;
            }
            const job = await waitForImport(accepted.statusUrl);
            const count = job.result?.results?.length || 0;
            status.textContent = `Import complete (${count} datasets, ${job.result?.durationSeconds ?? "?"} seconds).`;
            await loadHotels();
        }
        catch (error) { showError(error); }
        finally { importButton.disabled=false; }
    }
    importButton.onclick=runImport;
    document.querySelectorAll(".settings-nav button").forEach(button => {
        button.onclick = () => showSection(button.dataset.section);
    });
    document.querySelectorAll("[data-add]").forEach(button=>button.onclick=()=>{const key=button.dataset.add;model[key].push(structuredClone(defaults[key]));key==="distributionGroups"?renderDistribution():renderRows(key);setDirty(true)});
    hotel.onchange=()=>{if(dirty&&!confirm("Discard unsaved changes?")){hotel.value=loadedEnterpriseId;return}loadSettings(hotel.value)};form.addEventListener("input",()=>setDirty(true));form.onsubmit=submit;window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue=""}});loadHotels();
}());
