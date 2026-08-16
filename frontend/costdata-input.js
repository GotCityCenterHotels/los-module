(function () {
    "use strict";
    const API = "/api/costdata/settings";
    const PROPERTIES_API = "/api/costdata/properties";
    const SOURCES_API = "/api/costdata/sources";
    const AGENCIES_API = "/api/costdata/agencies";
    const RATES_API = "/api/costdata/rates";
    const hotel = document.getElementById("settingsHotel"), form = document.getElementById("settingsForm");
    const layout = document.getElementById("settingsLayout"), status = document.getElementById("settingsStatus");
    const errorPanel = document.getElementById("settingsError"), save = document.getElementById("saveSettings");
    const dirtyState = document.getElementById("dirtyState"), importButton = document.getElementById("runImportButton");
    let model = null, dirty = false, loadedEnterpriseId = "";

    if (typeof LosFormat === "undefined") {
        throw new Error(
            "los-format.js did not load - hard refresh the page, and check that "
            + "the script deployed alongside costdata-input.js."
        );
    }

    // Whole-number fields (guest counts, arrival counts) stay integers. SEK
    // fields are whole kronor and are owned by the shared LosFormat input
    // component; everything left over (hours, minutes, percentages) is not
    // money and keeps two decimals.
    const INTEGER_FIELDS = new Set([
        "minGuests", "maxGuests", "minArrivals", "maxArrivals"
    ]);
    // Profile fields backed by a checkbox rather than a value. They round-trip
    // through .checked, not .value - an unchecked box has no value at all.
    const CHECKBOX_FIELDS = new Set(["arrivalCostEnabled", "franchiseEnabled"]);
    const DECIMALS = 2;

    // Each row is [field, label, type]. Optional fourth entry is a row class.
    const configs = {
        arrivalTiers: [["minArrivals","Min arrivals","number"],["maxArrivals","Max arrivals","number"],["receptionHours","Reception hours","number"]],
        breakfastTiers: [["minGuests","From guests","number"],["maxGuests","To guests","number"],["staffHours","Staff hours","number"]]
    };
    const rowClasses = { breakfastTiers: "tier-row is-compact" };
    const defaults = { arrivalTiers:{minArrivals:0,maxArrivals:"",receptionHours:0}, breakfastTiers:{minGuests:0,maxGuests:"",staffHours:0} };

    // Rates, channels, origins and room categories for the selected hotel, from
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
            sources = {
                rates: [], channels: [], cleaningCategories: [], origins: [],
                capabilities: {}, error: error.message
            };
            console.warn("Source lookup failed; manual entry still available.", error);
        }
    }

    function sourceCapability(name) {
        return Boolean(sources && sources.capabilities && sources.capabilities[name]);
    }

    function toFixedDecimals(value) {
        if (value === "" || value === null || value === undefined) return "";
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed.toFixed(DECIMALS) : value;
    }
    // The display value for a field, before the user touches it: SEK fields
    // are whole kronor, integers are left alone, everything else is fixed at
    // two decimals.
    function displayValue(field, value) {
        if (LosFormat.isMoneyField(field)) return LosFormat.normalizeSekInputValue(value);
        if (INTEGER_FIELDS.has(field)) return value ?? "";
        return toFixedDecimals(value);
    }
    // Applied on blur rather than on input, so typing "1.5" is not rewritten to
    // "1.50" mid-keystroke and the caret does not jump. SEK fields are handed
    // to the shared component instead, so whole-krona rounding is defined once
    // for the whole application.
    function bindNumberNormalisation(input, field, onChange) {
        if (input.type !== "number") return;
        if (LosFormat.isMoneyField(field)) {
            LosFormat.bindSekInput(input, onChange);
            return;
        }
        if (INTEGER_FIELDS.has(field)) return;
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
                // Reopening the page on the property last edited also avoids
                // loading a hotel nobody asked for and then loading the right
                // one, which was two full source lookups instead of one.
                const remembered = rememberedProperty();
                hotel.value = properties.some(
                    (property) => String(property.enterpriseId) === remembered
                ) ? remembered : String(properties[0].enterpriseId);
                await loadSettings(hotel.value);
            }
            else {
                layout.hidden = true;
                status.textContent = "No properties were found in the source or imported cost data.";
                hotel.disabled = false;
            }
        }
        catch (error) { hotel.disabled = false; showError(error, "Loading properties"); }
    }

    const PROPERTY_STORAGE = "costdata-input-property";
    function rememberedProperty() {
        try { return localStorage.getItem(PROPERTY_STORAGE) || ""; } catch { return ""; }
    }
    function rememberProperty(enterpriseId) {
        try { localStorage.setItem(PROPERTY_STORAGE, enterpriseId); } catch { /* private mode */ }
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
            model.distributionOriginGroups = model.distributionOriginGroups || [];
            model.bedTypes = model.bedTypes || [];
            loadedEnterpriseId = model.enterpriseId;
            rememberProperty(loadedEnterpriseId);
            rateCache.clear();
            agencyCache.clear();
            originDrafts = new WeakMap();
            render();
            layout.hidden = false;
            setDirty(false);
            if (sources && sources.error) {
                // Rates, channels, origins and room categories all come from
                // this one call, so a failure here empties every picker. Say so
                // plainly instead of leaving empty dropdowns with no explanation.
                showError(
                    new Error(
                        `${sources.error} Rates, origins and room categories are unavailable, `
                        + "so those fields must be typed manually."
                    ),
                    "Loading this hotel's rates and room categories"
                );
                status.textContent = `Editing ${model.hotelName} - source lists unavailable`;
            }
            else {
                status.textContent = `Editing ${model.hotelName}`;
            }
        }
        catch (error) { showError(error, "Loading this property's settings"); } finally { setBusy(false); }
    }
    function render() {
        for (const [key, value] of Object.entries(model.profile)) {
            const input = form.elements.namedItem(key);
            if (!input) continue;
            if (CHECKBOX_FIELDS.has(key)) {
                input.checked = isEnabled(value, key !== "franchiseEnabled");
                continue;
            }
            input.value = input.type === "number" ? displayValue(key, value) : value;
            bindNumberNormalisation(input, key);
        }
        // Each section renders independently. One section throwing used to take
        // down the whole page with a bare "Something went wrong", hiding both
        // the cause and every section that was fine.
        const failures = [];
        const sections = [
            ["Distribution", renderDistributionTree],
            ["Cleaning", renderCleaning],
            ...Object.keys(configs).map(key => [key, () => renderRows(key)])
        ];
        for (const [label, run] of sections) {
            try { run(); }
            catch (error) {
                failures.push(`${label}: ${error.message}`);
                console.error(`Failed to render the ${label} section`, error);
            }
        }
        syncSectionSwitches();
        if (failures.length) {
            showError(
                new Error(`${failures.join(" | ")}. The other sections are still editable.`),
                "Rendering the editor"
            );
        }
    }

    // A checkbox that has never been saved arrives as undefined, a saved one as
    // a real boolean. "false" is a truthy string, which is what this guards.
    function isEnabled(value, fallback) {
        if (value === undefined || value === null || value === "") return fallback;
        if (typeof value === "boolean") return value;
        return !["false", "0", "no", "off"].includes(String(value).trim().toLowerCase());
    }

    // ------------------------------------------------------------------
    // Section switches
    //
    // Switching a cost off greys its inputs out rather than hiding them: the
    // configuration is still there, still worth reading, and turning the switch
    // back on must not look like it lost anything.
    // ------------------------------------------------------------------
    function setSectionEnabled(section, enabled, keepEnabled) {
        if (!section) return;
        section.classList.toggle("is-switched-off", !enabled);
        for (const control of section.querySelectorAll("input, select, button")) {
            if (keepEnabled && keepEnabled(control)) continue;
            control.disabled = !enabled;
            // A disabled required field is skipped by constraint validation,
            // which is exactly right: an off section must never block a save.
        }
        const note = section.querySelector(".section-off-note");
        if (note) note.hidden = enabled;
    }

    function syncSectionSwitches() {
        const arrivals = document.querySelector('[data-settings-section="arrivals"]');
        const arrivalSwitch = form.elements.namedItem("arrivalCostEnabled");
        setSectionEnabled(
            arrivals,
            !arrivalSwitch || arrivalSwitch.checked,
            (control) => control === arrivalSwitch
        );

        const franchiseSwitch = form.elements.namedItem("franchiseEnabled");
        const franchiseFields = document.getElementById("franchiseFields");
        const franchiseOn = Boolean(franchiseSwitch && franchiseSwitch.checked);
        if (franchiseFields) {
            franchiseFields.classList.toggle("is-switched-off", !franchiseOn);
            for (const control of franchiseFields.querySelectorAll("input, select")) {
                control.disabled = !franchiseOn;
            }
            // VAT only participates in a gross calculation. Leaving it live on
            // a net basis invites someone to change a number that does nothing.
            const basis = form.elements.namedItem("franchiseBasis");
            const vat = form.elements.namedItem("franchiseVatPercent");
            if (vat) vat.disabled = !franchiseOn || !basis || basis.value !== "gross";
        }
        const offNote = document.getElementById("franchiseOffNote");
        if (offNote) offNote.hidden = franchiseOn;
    }

    // ======================================================================
    // Cleaning
    //
    // The property defines its bed types once, with the linen cost of making
    // each one up. A room category then says which beds are made up at each
    // occupancy, and the linen cost follows from that rather than being a
    // number retyped into every row.
    //
    // Most categories are cleaned the same way whatever the occupancy, so the
    // lowest occupancy carries the real setup and the rows above it inherit:
    // beds unless that row's override is switched on, minutes whenever its box
    // is left empty. The inherited figure is shown greyed in the empty box, so
    // it is always clear both what will be used and that typing is optional.
    // ======================================================================
    function bedTypeList() {
        return model.bedTypes || (model.bedTypes = []);
    }

    // Room rows are bound to a bed type by a key that lives only in this
    // editor, never by its name.
    //
    // The name is what gets persisted - it is the only reference that survives
    // a save, which rewrites every table and reissues every identity column -
    // but it is also the thing the operator is editing, one keystroke at a
    // time. Matching on it meant that typing "Sofa bed" next to an existing
    // "Sofa" re-pointed Sofa's rooms on the way past, that clearing the field
    // to retype it unbound every room permanently, and that deleting one of two
    // identically named beds stripped the other's rooms. Keys have none of
    // those failure modes; collect() writes the current names back out.
    let nextBedKey = 1;

    function ensureBedKeys() {
        for (const bed of bedTypeList()) {
            if (!bed.bedKey) bed.bedKey = `bed-${nextBedKey++}`;
        }
        for (const row of model.cleaningCategories || []) {
            for (const bed of row.beds || []) {
                if (bed.bedKey && bedByKey(bed.bedKey)) continue;
                const match = bedTypeList().find(
                    entry => String(entry.bedName || "").toLowerCase()
                        === String(bed.bedName || "").toLowerCase()
                );
                bed.bedKey = match ? match.bedKey : null;
            }
        }
    }

    function bedByKey(key) {
        return key ? bedTypeList().find(entry => entry.bedKey === key) : null;
    }

    // The bed type a room row points at, and what it is called right now. A row
    // whose bed type no longer exists keeps its saved name so it stays visible
    // and removable rather than vanishing from the editor.
    function resolveRowBed(rowBed) {
        const bed = bedByKey(rowBed.bedKey);
        return {
            bed,
            name: bed ? bed.bedName : rowBed.bedName,
            linenCost: bed ? numberValue(bed.linenCost) : 0
        };
    }

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    // What this row is actually made up with, following inheritance. Mirrors
    // _resolve_cleaning_inheritance on the server; the two must agree, because
    // the editor shows one and the Cost Data page costs the other.
    function baseRowFor(categoryName) {
        let base = null;
        for (const row of model.cleaningCategories || []) {
            if (String(row.categoryName).toLowerCase() !== String(categoryName).toLowerCase()) continue;
            if (!base || Number(row.occupancy) < Number(base.occupancy)) base = row;
        }
        return base;
    }

    function effectiveRow(row) {
        const base = baseRowFor(row.categoryName);
        const isBase = base === row;
        const inheritsBeds = !isBase && !row.overridesBase;
        const beds = ((inheritsBeds ? base : row) || {}).beds || [];
        const rawMinutes = row.cleaningMinutes;
        const inheritsMinutes = !isBase && (rawMinutes === null || rawMinutes === undefined || rawMinutes === "");
        const minutes = inheritsMinutes ? (base ? base.cleaningMinutes : null) : rawMinutes;
        // With no beds in play the row keeps its OWN linen cost, exactly as it
        // was costed before bed types existed. Taking the base's figure here
        // would re-cost every property the day this shipped, with no bed
        // configured anywhere to explain the change. Inheritance governs the
        // bed-derived cost only.
        const linen = beds.length
            ? beds.reduce(
                (total, bed) =>
                    total + resolveRowBed(bed).linenCost * (Number(bed.quantity) || 1), 0
            )
            : numberValue(row.linenCost);
        return {base, isBase, inheritsBeds, inheritsMinutes, beds, minutes, linen};
    }

    function renderBedTypes() {
        const root = document.getElementById("bedTypes");
        if (!root) return;
        root.replaceChildren();
        bedTypeList().forEach((bed, index) => {
            const row = document.createElement("div");
            row.className = "bed-type-row";

            const name = document.createElement("input");
            name.type = "text";
            name.value = bed.bedName || "";
            name.placeholder = "Double bed";
            name.setAttribute("aria-label", "Bed name");
            name.required = true;
            name.oninput = () => {
                // Rooms follow the bed by key, so a rename is just a rename -
                // nothing else in the model has to be rewritten to keep up.
                bed.bedName = name.value;
                setDirty(true);
                refreshCleaningTotals();
            };
            // The chips and "same as" summaries below carry the bed's name and
            // are only rebuilt by a render. Redrawing per keystroke would take
            // the focus out of this field, so they catch up when it is left.
            name.onchange = () => renderCleaning();

            const cost = document.createElement("input");
            cost.type = "number";
            cost.min = "0";
            cost.step = "1";
            cost.value = LosFormat.normalizeSekInputValue(bed.linenCost ?? 0);
            cost.setAttribute("aria-label", `${bed.bedName || "Bed"} linen cost`);
            cost.required = true;
            LosFormat.bindSekInput(cost, (value) => {
                bed.linenCost = value;
                refreshCleaningTotals();
            });
            cost.addEventListener("input", () => {
                bed.linenCost = cost.value;
                setDirty(true);
                refreshCleaningTotals();
            });

            const unit = document.createElement("span");
            unit.className = "bed-type-unit";
            unit.textContent = "kr";

            const remove = iconButton("×", `Remove ${bed.bedName || "bed type"}`);
            remove.classList.add("bed-type-remove");
            remove.onclick = () => {
                bedTypeList().splice(index, 1);
                removeBedEverywhere(bed.bedKey);
                renderCleaning();
                setDirty(true);
            };

            row.append(name, cost, unit, remove);
            root.append(row);
        });
        if (!bedTypeList().length) {
            const empty = document.createElement("p");
            empty.className = "tree-field-empty";
            empty.textContent =
                "No bed types yet. Add one to assign it to a room category below.";
            root.append(empty);
        }
    }

    // Only the rows pointing at THIS bed type. Filtering by name would take
    // out a second, identically named bed type's rooms along with it.
    function removeBedEverywhere(bedKey) {
        for (const row of model.cleaningCategories || []) {
            row.beds = (row.beds || []).filter(bed => bed.bedKey !== bedKey);
        }
    }

    // Every room category in this hotel, with one row per possible occupancy,
    // merged with what was saved. Categories come from the hotel in the Mews
    // ordering and that order is preserved. A saved row whose category no
    // longer exists is kept and marked rather than dropped with its costs.
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
                    // Blank, not zero: an occupancy with nothing typed in it
                    // takes the lowest occupancy's figure.
                    cleaningMinutes: existing ? existing.cleaningMinutes : null,
                    linenCost: existing ? existing.linenCost : 0,
                    overridesBase: existing ? Boolean(existing.overridesBase) : false,
                    beds: existing ? (existing.beds || []).map(bed => ({...bed})) : [],
                    fromHotel: true
                });
            }
        }
        for (const orphan of saved.values()) {
            merged.push({...orphan, beds: (orphan.beds || []).map(bed => ({...bed})), fromHotel: false});
        }
        model.cleaningCategories = merged;
        return merged;
    }

    function groupCategories(rows) {
        const groups = new Map();
        for (const row of rows) {
            const key = String(row.categoryName).toLowerCase();
            let group = groups.get(key);
            if (!group) {
                group = {name: row.categoryName, rows: [], fromHotel: row.fromHotel,
                    capacity: row.capacity, extraCapacity: row.extraCapacity};
                groups.set(key, group);
            }
            group.rows.push(row);
        }
        for (const group of groups.values()) {
            group.rows.sort((left, right) => Number(left.occupancy) - Number(right.occupancy));
        }
        return Array.from(groups.values());
    }

    function renderCleaning() {
        ensureBedKeys();
        renderBedTypes();
        const root = document.getElementById("cleaningCategories");
        if (!root) return;
        const rows = mergeCleaningWithHotel();
        root.replaceChildren();

        for (const group of groupCategories(rows)) {
            root.append(buildCleaningCategory(group));
        }

        if (!rows.length) {
            emptyMessage(root, sources && sources.error
                ? "Could not load this hotel's room categories."
                : "No room categories found for this hotel.");
        }
        refreshCleaningTotals();
    }

    // A category with no beds anywhere is the one thing an operator has to be
    // able to spot at a glance, so it is called out on the category and counted
    // in the section heading.
    function categoryIsSet(group) {
        return group.rows.some(row => (row.beds || []).length);
    }

    function refreshCleaningTotals() {
        const progress = document.getElementById("cleaningProgress");
        for (const node of document.querySelectorAll("[data-linen-for]")) {
            const row = (model.cleaningCategories || [])[Number(node.dataset.linenFor)];
            if (row) node.textContent = LosFormat.formatSekAmount(effectiveRow(row).linen);
        }
        for (const node of document.querySelectorAll("[data-minutes-for]")) {
            const row = (model.cleaningCategories || [])[Number(node.dataset.minutesFor)];
            if (!row) continue;
            const {minutes, inheritsMinutes} = effectiveRow(row);
            if (!inheritsMinutes) continue;
            node.placeholder = minutes === null || minutes === undefined || minutes === ""
                ? "0" : String(minutes);
        }
        if (!progress) return;
        const groups = groupCategories(model.cleaningCategories || []);
        const set = groups.filter(categoryIsSet).length;
        progress.textContent = groups.length
            ? `${set} of ${groups.length} categories have beds set`
            : "";
        progress.classList.toggle("is-incomplete", groups.length > 0 && set < groups.length);
    }

    function buildCleaningCategory(group) {
        const block = document.createElement("div");
        block.className = "cleaning-category";
        if (!group.fromHotel) block.classList.add("is-orphaned");

        const heading = document.createElement("div");
        heading.className = "cleaning-category-heading";
        const title = document.createElement("h3");
        title.textContent = group.name;
        const detail = document.createElement("span");
        detail.className = "cleaning-category-detail";
        detail.textContent = group.fromHotel
            ? `standard ${group.capacity}${group.extraCapacity ? ` + ${group.extraCapacity} extra` : ""}`
            : "no longer in this hotel";
        heading.append(title, detail);
        if (!categoryIsSet(group)) {
            const badge = document.createElement("span");
            badge.className = "cleaning-unset";
            badge.textContent = "No beds set";
            heading.append(badge);
        }
        block.append(heading);

        const table = document.createElement("div");
        table.className = "cleaning-grid";
        const header = document.createElement("div");
        header.className = "cleaning-grid-head";
        for (const label of ["Guests", "Beds", "Minutes", "Linen"]) {
            const cell = document.createElement("span");
            cell.textContent = label;
            header.append(cell);
        }
        table.append(header);
        for (const row of group.rows) table.append(buildCleaningRow(row, group));
        block.append(table);
        return block;
    }

    function buildCleaningRow(row, group) {
        const index = (model.cleaningCategories || []).indexOf(row);
        const state = effectiveRow(row);
        const line = document.createElement("div");
        line.className = "cleaning-grid-row";
        if (state.isBase) line.classList.add("is-base");

        const guests = document.createElement("span");
        guests.className = "cleaning-guests";
        guests.textContent = row.occupancy;
        line.append(guests);

        // --- Beds -----------------------------------------------------------
        const beds = document.createElement("div");
        beds.className = "cleaning-beds";
        if (state.inheritsBeds) {
            const inherited = document.createElement("span");
            inherited.className = "cleaning-inherited";
            inherited.textContent = state.beds.length
                ? `Same as ${state.base.occupancy} guest${Number(state.base.occupancy) === 1 ? "" : "s"}: ${describeBeds(state.beds)}`
                : `Same as ${state.base.occupancy} guest${Number(state.base.occupancy) === 1 ? "" : "s"}`;
            beds.append(inherited);
            const edit = document.createElement("button");
            edit.type = "button";
            edit.className = "text-button cleaning-override";
            edit.textContent = "Edit this count";
            edit.onclick = () => {
                // Start the override from what it was inheriting, so switching
                // it on never blanks the setup that was already in force.
                row.overridesBase = true;
                row.beds = state.beds.map(bed => ({...bed}));
                renderCleaning();
                setDirty(true);
            };
            beds.append(edit);
        }
        else {
            beds.append(buildBedChips(row));
            if (!state.isBase) {
                const revert = document.createElement("button");
                revert.type = "button";
                revert.className = "text-button cleaning-override";
                revert.textContent = "Inherit again";
                revert.onclick = () => {
                    row.overridesBase = false;
                    row.beds = [];
                    renderCleaning();
                    setDirty(true);
                };
                beds.append(revert);
            }
        }
        line.append(beds);

        // --- Minutes ----------------------------------------------------------
        const minutes = document.createElement("input");
        minutes.type = "number";
        minutes.min = "0";
        minutes.step = "0.01";
        minutes.className = "cleaning-minutes";
        minutes.setAttribute(
            "aria-label", `Cleaning minutes for ${row.categoryName} at ${row.occupancy} guests`
        );
        minutes.value = row.cleaningMinutes === null || row.cleaningMinutes === undefined
            ? "" : row.cleaningMinutes;
        if (state.isBase) {
            minutes.required = true;
            if (minutes.value === "") minutes.value = "0";
        }
        else {
            // The inherited figure sits in the box as a placeholder, so the
            // number that will be used is visible and it is obvious that typing
            // one here is optional.
            minutes.placeholder = state.minutes === null || state.minutes === undefined
                || state.minutes === "" ? "0" : String(state.minutes);
            minutes.dataset.minutesFor = String(index);
        }
        minutes.addEventListener("input", () => {
            row.cleaningMinutes = minutes.value === "" ? null : minutes.value;
            setDirty(true);
            refreshCleaningTotals();
        });
        minutes.addEventListener("blur", () => {
            if (minutes.value === "") return;
            const normalised = toFixedDecimals(minutes.value);
            if (normalised === minutes.value) return;
            minutes.value = normalised;
            row.cleaningMinutes = normalised;
        });
        line.append(minutes);

        // --- Linen ------------------------------------------------------------
        const linen = document.createElement("span");
        linen.className = "cleaning-linen";
        linen.dataset.linenFor = String(index);
        linen.textContent = LosFormat.formatSekAmount(state.linen);
        if (!state.beds.length && state.linen > 0) {
            linen.classList.add("is-legacy");
            linen.title =
                "Typed in before bed types existed. Assign beds to this category "
                + "and the linen cost will follow them.";
        }
        line.append(linen);

        // Marked and removable per ROW, not per category: a category that is
        // still in the hotel can lose an occupancy - Mews dropping an extra bed
        // shrinks 1-3 to 1-2 - and that stale row is otherwise indistinguishable
        // from a real one, cannot be deleted, and keeps skewing the blended
        // per-departure cost.
        if (!row.fromHotel) {
            line.classList.add("is-orphaned-row");
            const remove = iconButton("×", `Remove ${row.categoryName} at ${row.occupancy} guests`);
            remove.classList.add("cleaning-remove");
            remove.onclick = () => {
                const at = model.cleaningCategories.indexOf(row);
                if (at >= 0) model.cleaningCategories.splice(at, 1);
                renderCleaning();
                setDirty(true);
            };
            line.append(remove);
        }
        return line;
    }

    function describeBeds(beds) {
        return beds
            .map((bed) => {
                const name = resolveRowBed(bed).name || "(unnamed bed)";
                return (Number(bed.quantity) || 1) > 1
                    ? `${name} x${bed.quantity}` : name;
            })
            .join(", ");
    }

    function buildBedChips(row) {
        const wrap = document.createElement("div");
        wrap.className = "bed-chips";
        (row.beds || []).forEach((bed, bedIndex) => {
            const resolved = resolveRowBed(bed);
            const chip = document.createElement("span");
            chip.className = "bed-chip";
            if (!resolved.bed) chip.classList.add("is-unknown");
            const label = document.createElement("span");
            label.textContent = resolved.name || "(unnamed bed)";
            chip.append(label);

            const quantity = document.createElement("input");
            quantity.type = "number";
            quantity.min = "1";
            quantity.step = "1";
            quantity.className = "bed-chip-quantity";
            quantity.value = bed.quantity || 1;
            quantity.setAttribute("aria-label", `${resolved.name} count`);
            quantity.required = true;
            quantity.addEventListener("input", () => {
                bed.quantity = quantity.value;
                setDirty(true);
                refreshCleaningTotals();
            });
            // An emptied box is one bed, not "no number". Left as an empty
            // string it passed the browser's check, then failed the whole save
            // server-side with a message naming the bed rather than the box.
            quantity.addEventListener("blur", () => {
                if (quantity.value !== "" && Number(quantity.value) >= 1) return;
                quantity.value = "1";
                bed.quantity = 1;
                refreshCleaningTotals();
            });
            chip.append(quantity);

            const drop = iconButton("×", `Remove ${resolved.name}`, "rate-chip-remove");
            drop.onclick = () => {
                row.beds.splice(bedIndex, 1);
                renderCleaning();
                setDirty(true);
            };
            chip.append(drop);
            wrap.append(chip);
        });

        const available = bedTypeList().filter(bed =>
            String(bed.bedName || "").trim()
            && !(row.beds || []).some(existing => existing.bedKey === bed.bedKey)
        );
        if (available.length) {
            const picker = document.createElement("select");
            picker.className = "bed-add";
            picker.setAttribute("aria-label", "Add a bed to this room");
            picker.add(new Option("+ Bed", ""));
            for (const bed of available) picker.add(new Option(bed.bedName, bed.bedKey));
            picker.onchange = () => {
                const chosen = bedByKey(picker.value);
                if (!chosen) return;
                row.beds = row.beds || [];
                row.beds.push({
                    bedKey: chosen.bedKey, bedName: chosen.bedName, quantity: 1
                });
                renderCleaning();
                setDirty(true);
            };
            wrap.append(picker);
        }
        else if (!(row.beds || []).length) {
            const empty = document.createElement("span");
            empty.className = "tree-field-empty";
            empty.textContent = bedTypeList().length
                ? "All bed types are already on this row."
                : "Add a bed type above first.";
            wrap.append(empty);
        }
        return wrap;
    }

    // ======================================================================
    // Distribution tree
    //
    // Origin group -> travel agency subgroup -> rate group, each level with its
    // own percentage. The deeper a level matches, the more specific it is, so
    // its percentage wins over its parent's fallback.
    //
    // Every level is rebuilt from the model on change rather than patched in
    // place. The tree is small - a handful of groups per property - and a full
    // rebuild is the only way to keep the "already assigned" state on every
    // rate picker honest after an edit anywhere in the tree.
    // ======================================================================
    // Text typed into an origin group's free-text box but not yet added. Keyed
    // by the group object itself so it survives the rebuild and is collected
    // with the group when it is removed.
    let originDrafts = new WeakMap();

    function newOriginGroup() {
        return {groupName: "", fallbackPercent: 0, origins: [], agencyGroups: []};
    }
    function newAgencyGroup() {
        return {groupName: "", fallbackPercent: 0, filters: [], rateGroups: []};
    }
    function newRateGroup() {
        return {groupName: "", costPercent: 0, rates: []};
    }

    function labelledInput(labelText, options) {
        const wrap = document.createElement("label");
        wrap.textContent = labelText;
        const input = document.createElement("input");
        Object.assign(input, options || {});
        wrap.append(input);
        return {wrap, input};
    }

    function iconButton(text, ariaLabel, className) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className || "remove-rule";
        button.textContent = text;
        button.setAttribute("aria-label", ariaLabel);
        return button;
    }

    function textButton(text, onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "text-button";
        button.textContent = text;
        button.onclick = onClick;
        return button;
    }

    function levelWarning(text) {
        const note = document.createElement("p");
        note.className = "tree-warning";
        note.textContent = text;
        return note;
    }

    function renderDistributionTree() {
        const root = document.getElementById("distributionOriginGroups");
        if (!root) return;
        root.replaceChildren();
        model.distributionOriginGroups.forEach((group, originIndex) => {
            root.append(buildOriginGroup(group, originIndex));
        });
        emptyMessage(
            root,
            "No origin groups yet. Every reservation is charged the fallback "
            + "distribution % above until you add one."
        );
    }

    function buildOriginGroup(group, originIndex) {
        const row = document.createElement("div");
        row.className = "rule-row tree-group tree-origin-group";

        const main = document.createElement("div");
        main.className = "rule-main";
        const name = labelledInput("Origin group name", {
            type: "text", value: group.groupName || "", required: true
        });
        name.input.oninput = () => {
            group.groupName = name.input.value;
            setDirty(true);
        };
        const percent = labelledInput("Fallback %", {
            type: "number", min: "0", max: "100", step: "0.01",
            value: toFixedDecimals(group.fallbackPercent), required: true
        });
        bindNumberNormalisation(percent.input, "fallbackPercent", (value) => {
            group.fallbackPercent = value;
        });
        percent.input.oninput = () => {
            group.fallbackPercent = percent.input.value;
            setDirty(true);
        };
        const remove = iconButton("Remove", "Remove origin group");
        remove.onclick = () => {
            model.distributionOriginGroups.splice(originIndex, 1);
            renderDistributionTree();
            setDirty(true);
        };
        main.append(name.wrap, percent.wrap, remove);
        row.append(main);

        row.append(buildOriginPicker(group));

        const subgroups = document.createElement("div");
        subgroups.className = "tree-children tree-agency-list";
        (group.agencyGroups || []).forEach((agency, agencyIndex) => {
            subgroups.append(buildAgencyGroup(group, agency, originIndex, agencyIndex));
        });
        if (!(group.agencyGroups || []).length) {
            const empty = document.createElement("p");
            empty.className = "tree-empty";
            empty.textContent =
                "No travel agency subgroups. Everything in this origin group is "
                + "charged its fallback %.";
            subgroups.append(empty);
        }
        row.append(subgroups);
        row.append(textButton("+ Add travel agency subgroup", () => {
            group.agencyGroups = group.agencyGroups || [];
            group.agencyGroups.push(newAgencyGroup());
            renderDistributionTree();
            setDirty(true);
        }));

        if (!(group.origins || []).length && !(group.agencyGroups || []).length) {
            row.append(levelWarning(
                "This group has no origins and no subgroups, so it matches "
                + "nothing. Pick an origin below or remove the group."
            ));
        }
        return row;
    }

    // Origins are a short, closed list per property, so checkboxes beat a
    // combo: everything available is visible at once and the count next to each
    // says which ones the property actually books through.
    function buildOriginPicker(group) {
        const field = document.createElement("div");
        field.className = "tree-field";
        const heading = document.createElement("span");
        heading.className = "tree-field-label";
        heading.textContent = "Origins";
        field.append(heading);

        const available = (sources && sources.origins) || [];
        const selected = new Set((group.origins || []).map(value => value.toLowerCase()));
        // A saved origin the source no longer reports still has to be visible
        // and removable - dropping it silently would change the rulebook.
        const orphans = (group.origins || []).filter(value =>
            !available.some(option => option.name.toLowerCase() === value.toLowerCase())
        );

        const choices = document.createElement("div");
        choices.className = "compact-checks origin-choices";
        for (const option of available) {
            choices.append(originChoice(group, option.name, option.reservationCount, selected));
        }
        for (const value of orphans) {
            choices.append(originChoice(group, value, null, selected, true));
        }
        if (!available.length && !orphans.length) {
            const empty = document.createElement("span");
            empty.className = "tree-field-empty";
            empty.textContent = sourceCapability("origin")
                ? "This hotel has no reservation origins in the source window."
                : "Origins are unavailable for this hotel, so type one below.";
            choices.append(empty);
        }
        field.append(choices);

        // Free text stays available whatever the source says: a mirror without
        // an origin column must not make the whole level unusable.
        const manual = document.createElement("div");
        manual.className = "tree-manual";
        const entry = document.createElement("input");
        entry.type = "text";
        entry.placeholder = "Add an origin by name...";
        entry.setAttribute("aria-label", "Add an origin by name");
        // Half-typed text is not in the model, and every edit anywhere in the
        // tree rebuilds this input from scratch - so ticking a checkbox used to
        // throw away whatever was being typed here, with no sign it had gone.
        entry.value = originDrafts.get(group) || "";
        entry.addEventListener("input", () => {
            originDrafts.set(group, entry.value);
        });
        const add = document.createElement("button");
        add.type = "button";
        add.className = "secondary-button";
        add.textContent = "Add origin";
        add.onclick = () => {
            const value = entry.value.trim();
            if (!value) return;
            group.origins = group.origins || [];
            if (!group.origins.some(existing => existing.toLowerCase() === value.toLowerCase())) {
                group.origins.push(value);
            }
            originDrafts.delete(group);
            renderDistributionTree();
            setDirty(true);
        };
        entry.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            // Otherwise Enter submits the whole form and the typed origin is lost.
            event.preventDefault();
            add.click();
        });
        manual.append(entry, add);
        field.append(manual);
        return field;
    }

    function originChoice(group, value, count, selected, orphaned) {
        const label = document.createElement("label");
        if (orphaned) label.classList.add("is-orphaned-choice");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = value;
        box.checked = selected.has(value.toLowerCase());
        box.onchange = () => {
            group.origins = (group.origins || []).filter(
                existing => existing.toLowerCase() !== value.toLowerCase()
            );
            if (box.checked) group.origins.push(value);
            renderDistributionTree();
            setDirty(true);
        };
        label.append(box, document.createTextNode(` ${value}`));
        if (count !== null && count !== undefined) {
            const badge = document.createElement("small");
            badge.textContent = integerLabel(count);
            label.append(badge);
        }
        if (orphaned) {
            const badge = document.createElement("small");
            badge.textContent = "not in source";
            label.append(badge);
        }
        return label;
    }

    const integerFormatter = new Intl.NumberFormat("en-SE", {maximumFractionDigits: 0});
    function integerLabel(count) {
        return integerFormatter.format(Number(count) || 0);
    }

    function buildAgencyGroup(originGroup, agency, originIndex, agencyIndex) {
        const row = document.createElement("div");
        row.className = "tree-group tree-agency-group";

        const main = document.createElement("div");
        main.className = "rule-main";
        const name = labelledInput("Subgroup name", {
            type: "text", value: agency.groupName || "", required: true
        });
        name.input.oninput = () => { agency.groupName = name.input.value; setDirty(true); };
        const percent = labelledInput("Fallback %", {
            type: "number", min: "0", max: "100", step: "0.01",
            value: toFixedDecimals(agency.fallbackPercent), required: true
        });
        bindNumberNormalisation(percent.input, "fallbackPercent", (value) => {
            agency.fallbackPercent = value;
        });
        percent.input.oninput = () => {
            agency.fallbackPercent = percent.input.value;
            setDirty(true);
        };
        const remove = iconButton("Remove", "Remove travel agency subgroup");
        remove.onclick = () => {
            originGroup.agencyGroups.splice(agencyIndex, 1);
            renderDistributionTree();
            setDirty(true);
        };
        main.append(name.wrap, percent.wrap, remove);
        row.append(main);

        const filters = document.createElement("div");
        filters.className = "tree-field";
        const heading = document.createElement("span");
        heading.className = "tree-field-label";
        heading.textContent = "Travel agency contains";
        filters.append(heading);
        (agency.filters || []).forEach((rule, filterIndex) => {
            filters.append(buildAgencyFilter(originGroup, agency, rule, filterIndex));
        });
        if (!(agency.filters || []).length) {
            const empty = document.createElement("span");
            empty.className = "tree-field-empty";
            empty.textContent = "No search terms yet.";
            filters.append(empty);
        }
        filters.append(textButton("+ Add search term", () => {
            agency.filters = agency.filters || [];
            agency.filters.push({matchField: "travelAgency", containsValue: ""});
            renderDistributionTree();
            setDirty(true);
        }));
        row.append(filters);

        const rateGroups = document.createElement("div");
        rateGroups.className = "tree-children tree-rate-list";
        (agency.rateGroups || []).forEach((rateGroup, rateGroupIndex) => {
            rateGroups.append(buildRateGroup(
                originGroup, agency, rateGroup,
                {originIndex, agencyIndex, rateGroupIndex}
            ));
        });
        if (!(agency.rateGroups || []).length) {
            const empty = document.createElement("p");
            empty.className = "tree-empty";
            empty.textContent =
                "No rate groups. Everything this subgroup matches is charged its "
                + "fallback %.";
            rateGroups.append(empty);
        }
        row.append(rateGroups);
        row.append(textButton("+ Add rate group", () => {
            agency.rateGroups = agency.rateGroups || [];
            agency.rateGroups.push(newRateGroup());
            renderDistributionTree();
            setDirty(true);
        }));

        if (!(agency.filters || []).some(rule => String(rule.containsValue || "").trim())
            && !(agency.rateGroups || []).length) {
            row.append(levelWarning(
                "This subgroup has no search term and no rate groups, so it "
                + "matches nothing."
            ));
        }
        return row;
    }

    // The search is a "contains" match applied without regard to case, so the
    // suggestions have to come from the same place the cost algorithm will look
    // - the agencies this hotel actually has reservations from.
    function buildAgencyFilter(originGroup, agency, rule, filterIndex) {
        const line = document.createElement("div");
        line.className = "tree-filter-row";

        const combo = document.createElement("div");
        combo.className = "combo";
        const input = document.createElement("input");
        input.type = "text";
        input.className = "combo-input";
        input.value = rule.containsValue || "";
        input.placeholder = "Part of the agency name...";
        input.setAttribute("aria-label", "Travel agency contains");
        input.setAttribute("autocomplete", "off");

        const popup = document.createElement("div");
        popup.className = "combo-popup";
        popup.setAttribute("role", "listbox");
        popup.hidden = true;

        const api = {root: combo, close: () => { popup.hidden = true; }};

        async function suggest() {
            const term = input.value.trim();
            if (!sourceCapability("travelAgency") || term.length < 2) {
                popup.hidden = true;
                return;
            }
            const matches = await searchAgencies(term, originGroup.origins);
            // The field may have moved on while the request was in flight.
            if (input.value.trim() !== term) return;
            popup.replaceChildren();
            if (!matches.length) {
                const empty = document.createElement("div");
                empty.className = "combo-empty";
                empty.textContent =
                    "No agency in this hotel's reservations contains that. The term "
                    + "is still saved and matched when the cost is calculated.";
                popup.append(empty);
            }
            for (const match of matches) {
                const option = document.createElement("div");
                option.className = "combo-option";
                option.setAttribute("role", "option");
                const label = document.createElement("span");
                label.className = "combo-option-name";
                label.textContent = match.name;
                const badge = document.createElement("span");
                badge.className = "combo-option-group";
                badge.textContent = `${integerLabel(match.reservationCount)} res.`;
                option.append(label, badge);
                option.addEventListener("pointerdown", (event) => {
                    event.preventDefault();
                    input.value = match.name;
                    rule.containsValue = match.name;
                    popup.hidden = true;
                    openCombo = null;
                    setDirty(true);
                });
                popup.append(option);
            }
            // Close whatever was open BEFORE unhiding this one. The other way
            // round, the second keystroke finds this combo registered as the
            // open one and closes it again - so refining the search, which is
            // the entire point of the control, permanently hid the list.
            closeOpenCombo();
            popup.hidden = false;
            openCombo = api;
        }

        const debouncedSuggest = debounce(suggest, 250);
        input.addEventListener("input", () => {
            rule.containsValue = input.value.trim();
            setDirty(true);
            debouncedSuggest();
        });
        input.addEventListener("focus", debouncedSuggest);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !popup.hidden) {
                event.stopPropagation();
                popup.hidden = true;
                openCombo = null;
            }
            // Enter in a text field inside a form submits it. Here it should
            // only dismiss the suggestion list.
            if (event.key === "Enter") { event.preventDefault(); popup.hidden = true; }
        });

        combo.append(input, popup);
        const remove = iconButton("×", "Remove search term");
        remove.onclick = () => {
            agency.filters.splice(filterIndex, 1);
            renderDistributionTree();
            setDirty(true);
        };
        line.append(combo, remove);
        return line;
    }

    function buildRateGroup(originGroup, agency, rateGroup, path) {
        const row = document.createElement("div");
        row.className = "tree-group tree-rate-group";

        const main = document.createElement("div");
        main.className = "rule-main";
        const name = labelledInput("Rate group name", {
            type: "text", value: rateGroup.groupName || "", required: true
        });
        name.input.oninput = () => { rateGroup.groupName = name.input.value; setDirty(true); };
        const percent = labelledInput("Distribution %", {
            type: "number", min: "0", max: "100", step: "0.01",
            value: toFixedDecimals(rateGroup.costPercent), required: true
        });
        bindNumberNormalisation(percent.input, "costPercent", (value) => {
            rateGroup.costPercent = value;
        });
        percent.input.oninput = () => {
            rateGroup.costPercent = percent.input.value;
            setDirty(true);
        };
        const remove = iconButton("Remove", "Remove rate group");
        remove.onclick = () => {
            agency.rateGroups.splice(path.rateGroupIndex, 1);
            renderDistributionTree();
            setDirty(true);
        };
        main.append(name.wrap, percent.wrap, remove);
        row.append(main);

        const chips = document.createElement("div");
        chips.className = "rate-chips";
        (rateGroup.rates || []).forEach((rate, rateIndex) => {
            const chip = document.createElement("span");
            chip.className = "rate-chip";
            chip.append(document.createTextNode(rate.rateName));
            const drop = iconButton("×", `Remove ${rate.rateName}`, "rate-chip-remove");
            drop.onclick = () => {
                rateGroup.rates.splice(rateIndex, 1);
                renderDistributionTree();
                setDirty(true);
            };
            chip.append(drop);
            chips.append(chip);
        });
        if (!(rateGroup.rates || []).length) {
            const empty = document.createElement("span");
            empty.className = "tree-field-empty";
            empty.textContent = "No rates yet - this group is not saved until it has one.";
            chips.append(empty);
        }
        row.append(chips);

        const actions = document.createElement("div");
        actions.className = "rate-actions";
        actions.append(
            buildRatePicker(
                "matching", "+ Add matching rate", originGroup, agency, rateGroup, path
            ),
            buildRatePicker(
                "all", "+ Add any rate on the property",
                originGroup, agency, rateGroup, path
            )
        );
        row.append(actions);
        return row;
    }

    // Two pickers, one component. "matching" only offers rates that reservations
    // under this branch's origin and agency filters were actually sold on, which
    // is what makes the list short enough to be useful; "all" is the escape
    // hatch for a rate that has not been sold yet, and is deliberately a
    // separate button so the narrowed list is never quietly replaced by the
    // full one.
    function buildRatePicker(mode, label, originGroup, agency, rateGroup, path) {
        const combo = document.createElement("div");
        combo.className = "combo rate-picker";

        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "text-button";
        trigger.textContent = label;

        const popup = document.createElement("div");
        popup.className = "combo-popup rate-popup";
        popup.setAttribute("role", "listbox");
        popup.hidden = true;

        const search = document.createElement("input");
        search.type = "text";
        search.className = "combo-input rate-search";
        search.placeholder = "Search rates...";
        search.setAttribute("aria-label", `Search ${mode === "all" ? "all" : "matching"} rates`);
        search.setAttribute("autocomplete", "off");

        const list = document.createElement("div");
        list.className = "rate-options";

        const note = document.createElement("small");
        note.className = "match-note";

        const api = {root: combo, close};
        let options = [];

        function sourceRateId(name) {
            const match = options.find(
                option => option.name.toLowerCase() === name.toLowerCase()
            );
            return (match && match.id) || null;
        }

        function close() {
            popup.hidden = true;
            search.value = "";
        }

        function draw() {
            const assigned = CostMatch.rateAssignmentIndex(
                model.distributionOriginGroups, path
            );
            // Rates already in this very group are not "available" either; they
            // are simply already picked.
            const own = new Set(
                (rateGroup.rates || []).map(rate => rate.rateName.toLowerCase())
            );
            const selectable = options.filter(option => !own.has(option.name.toLowerCase()));
            const {free, taken} = CostMatch.partitionOptions(
                selectable, assigned, search.value
            );

            list.replaceChildren();
            for (const item of free) list.append(rateOption(item, null));
            if (taken.length) {
                const heading = document.createElement("div");
                heading.className = "combo-section";
                heading.textContent = "Already assigned";
                list.append(heading);
                for (const item of taken) list.append(rateOption(item, item.owner));
            }
            if (!free.length && !taken.length) {
                const empty = document.createElement("div");
                empty.className = "combo-empty";
                empty.textContent = options.length
                    ? "No rate matches that search."
                    : emptyReason();
                list.append(empty);
            }
            note.textContent = options.length
                ? `${free.length} available, ${taken.length} already assigned`
                : "";
        }

        function emptyReason() {
            if (sources && sources.error) return "This hotel's rate list could not be loaded.";
            if (mode === "all") return "This hotel has no rates in the source.";
            if (!sourceCapability("rateFromReservations")) {
                return "Reservations in this mirror carry no rate, so matching "
                    + "rates cannot be narrowed. Use \"Add any rate\" instead.";
            }
            return "No reservations under these filters were sold on a rate.";
        }

        function rateOption(item, owner) {
            const option = document.createElement("div");
            option.className = "combo-option";
            option.setAttribute("role", "option");
            const name = document.createElement("span");
            name.className = "combo-option-name";
            name.textContent = item.name;
            option.append(name);
            if (owner) {
                option.classList.add("is-assigned");
                option.setAttribute("aria-disabled", "true");
                const badge = document.createElement("span");
                badge.className = "combo-option-group";
                badge.textContent = owner;
                option.append(badge);
                return option;
            }
            option.addEventListener("pointerdown", (event) => {
                event.preventDefault();
                rateGroup.rates = rateGroup.rates || [];
                rateGroup.rates.push({
                    // partitionOptions rebuilds its results as {name} / {name,
                    // owner} and drops everything else, so the Mews id has to
                    // be looked back up from the list it came from. Without
                    // this every saved rate stored a null id and a rate
                    // renamed in Mews could never be reconciled again.
                    rateId: sourceRateId(item.name), rateName: item.name
                });
                closeOpenCombo();
                renderDistributionTree();
                setDirty(true);
            });
            return option;
        }

        trigger.onclick = async () => {
            if (!popup.hidden) { close(); openCombo = null; return; }
            closeOpenCombo();
            popup.hidden = false;
            openCombo = api;
            list.replaceChildren(loadingRow());
            options = mode === "all"
                ? ((sources && sources.rates) || [])
                : await matchingRates(originGroup.origins, agency.filters);
            if (popup.hidden) return;
            draw();
            search.focus();
        };
        search.addEventListener("input", draw);
        search.addEventListener("keydown", (event) => {
            if (event.key === "Enter") event.preventDefault();
            if (event.key === "Escape") { event.stopPropagation(); close(); openCombo = null; }
        });

        popup.append(search, list, note);
        combo.append(trigger, popup);
        return combo;
    }

    function loadingRow() {
        const row = document.createElement("div");
        row.className = "combo-empty";
        row.textContent = "Looking up rates...";
        return row;
    }

    // ------------------------------------------------------------------
    // Source lookups for the tree
    // ------------------------------------------------------------------
    const rateCache = new Map();
    const agencyCache = new Map();

    function debounce(run, delayMs) {
        let timer = null;
        return (...parameters) => {
            clearTimeout(timer);
            timer = setTimeout(() => run(...parameters), delayMs);
        };
    }

    function originParameter(origins) {
        return (origins || []).join(",");
    }

    async function searchAgencies(term, origins) {
        // The property has to be part of the key. Without it, searching "boo"
        // on one hotel served the next hotel's agency names, with the first
        // hotel's reservation counts.
        const key = `${loadedEnterpriseId}|${originParameter(origins)}|${term.toLowerCase()}`;
        if (agencyCache.has(key)) return agencyCache.get(key);
        try {
            const parameters = new URLSearchParams({search: term});
            if (origins && origins.length) parameters.set("origins", originParameter(origins));
            const payload = await LosApi.fetchJson(
                `${AGENCIES_API}/${encodeURIComponent(loadedEnterpriseId)}?${parameters}`
            );
            const matches = payload.data || [];
            agencyCache.set(key, matches);
            return matches;
        }
        catch (error) {
            console.warn("Travel agency lookup failed", error);
            return [];
        }
    }

    // One request per search term, merged. The API narrows by a single term,
    // and a subgroup with two terms matches the union of both - so the picker
    // has to show the union too, or it would hide rates the subgroup covers.
    async function matchingRates(origins, filters) {
        const terms = (filters || [])
            .map(rule => String(rule.containsValue || "").trim())
            .filter(Boolean);
        const key = `${originParameter(origins)}|${terms.join("|").toLowerCase()}`;
        if (rateCache.has(key)) return rateCache.get(key);

        const requests = (terms.length ? terms : [""]).map(async (term) => {
            const parameters = new URLSearchParams();
            if (origins && origins.length) parameters.set("origins", originParameter(origins));
            if (term) parameters.set("agency", term);
            const query = parameters.toString();
            const payload = await LosApi.fetchJson(
                `${RATES_API}/${encodeURIComponent(loadedEnterpriseId)}${query ? `?${query}` : ""}`
            );
            return (payload.data && payload.data.rates) || [];
        });

        try {
            const merged = new Map();
            for (const rates of await Promise.all(requests)) {
                for (const rate of rates) {
                    if (!merged.has(rate.name.toLowerCase())) merged.set(rate.name.toLowerCase(), rate);
                }
            }
            const result = Array.from(merged.values()).sort(
                (left, right) => left.name.localeCompare(right.name)
            );
            rateCache.set(key, result);
            return result;
        }
        catch (error) {
            console.warn("Matching rate lookup failed", error);
            return [];
        }
    }

    // Only one popup may be open at a time; a single document listener closes it
    // rather than each row registering its own (which would leak on re-render).
    let openCombo = null;
    function closeOpenCombo() {
        if (openCombo) { openCombo.close(); openCombo = null; }
    }
    document.addEventListener("pointerdown", (event) => {
        if (openCombo && !openCombo.root.contains(event.target)) closeOpenCombo();
    });

    function renderRows(key) {
        const root = document.getElementById(key);
        if (!root) return;
        root.replaceChildren();
        model[key].forEach((item, index) => {
            const row = document.createElement("div");
            row.className = `rule-row${rowClasses[key] ? ` ${rowClasses[key]}` : ""}`;
            for (const [field, label, type] of configs[key]) {
                const wrap = document.createElement("label");
                wrap.textContent = label;
                const input = document.createElement("input");
                input.type = type;
                if (type === "number") {
                    input.min = "0";
                    input.step = INTEGER_FIELDS.has(field) ? "1" : "0.01";
                    input.value = displayValue(field, item[field]);
                }
                else {
                    input.value = item[field] ?? "";
                }
                input.dataset.field = field;
                bindNumberNormalisation(input, field, (value) => { item[field] = value; });
                wrap.append(input);
                row.append(wrap);
            }
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "remove-rule";
            // A compact threshold row has no space for a word; the label stays
            // on the button for assistive technology.
            remove.textContent = rowClasses[key] ? "×" : "Remove";
            remove.setAttribute("aria-label", "Remove threshold");
            remove.onclick = () => removeRow(key, index);
            row.append(remove);
            bindFields(row, item);
            root.append(row);
        });
        emptyMessage(root, "No rules added yet.");
    }
    function bindFields(root,item){root.querySelectorAll("[data-field]").forEach(input=>input.addEventListener("input",()=>{item[input.dataset.field]=input.type==="checkbox"?input.checked:input.value;setDirty(true)}))}
    function emptyMessage(root,text){if(!root.children.length){const p=document.createElement("p");p.className="rules-empty";p.textContent=text;root.append(p)}}
    function removeRow(key,index){model[key].splice(index,1);renderRows(key);setDirty(true);syncSectionSwitches()}
    function collect() {
        // Money is rounded once here as well as on blur, so a value typed and
        // submitted with Enter (never blurred) is stored at the same precision
        // it is displayed at. The backend rounds again as the last word.
        for (const input of form.querySelectorAll("[name]")) {
            if (CHECKBOX_FIELDS.has(input.name)) {
                model.profile[input.name] = input.checked;
                continue;
            }
            // A switched-off section's inputs are disabled, which exempts them
            // from constraint validation - so a required field left blank in an
            // off section reaches the save. Sending its empty string would fail
            // the whole PUT on a field the page has greyed out, which is the
            // opposite of what switching a cost off should do. The last known
            // value stands instead.
            if (input.disabled && input.value === "") continue;
            model.profile[input.name] = LosFormat.isMoneyField(input.name)
                ? LosFormat.normalizeSekInputValue(input.value)
                : input.value;
        }
        for (const row of model.cleaningCategories || []) {
            if (row.linenCost === "" || row.linenCost === null || row.linenCost === undefined) continue;
            row.linenCost = LosFormat.normalizeSekInputValue(row.linenCost);
        }
        for (const bed of model.bedTypes || []) {
            bed.linenCost = LosFormat.normalizeSekInputValue(bed.linenCost);
        }
        // The key is how the editor tracks a bed type; the name is what the
        // database stores. This is where the two are reconciled, once, so a
        // rename reaches every room that uses the bed without any of them
        // having been rewritten while it was being typed.
        for (const row of model.cleaningCategories || []) {
            for (const bed of row.beds || []) {
                const resolved = resolveRowBed(bed);
                if (resolved.bed) bed.bedName = resolved.bed.bedName;
            }
        }
        // A half-finished row carries no meaning and the backend rejects a
        // blank value outright, which previously failed the entire save. An
        // empty search term is likewise a row someone started and abandoned,
        // not a filter that matches everything.
        //
        // The pruning builds a copy rather than editing the model in place. It
        // used to splice the model's own arrays, so a save the backend rejected
        // left the still-mounted rows holding indices into an array that had
        // shrunk underneath them - and removing the blank row then deleted its
        // neighbour's typed value instead.
        return {
            ...model,
            bedTypes: (model.bedTypes || []).filter(
                bed => String(bed.bedName || "").trim()
            ),
            distributionGroups: (model.distributionGroups || []).map(group => ({
                ...group,
                rules: (group.rules || []).filter(
                    rule => String(rule.matchValue || "").trim()
                )
            })),
            distributionOriginGroups: (model.distributionOriginGroups || []).map(group => ({
                ...group,
                agencyGroups: (group.agencyGroups || []).map(agency => ({
                    ...agency,
                    filters: (agency.filters || []).filter(
                        rule => String(rule.containsValue || "").trim()
                    )
                }))
            }))
        };
    }

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
            model.distributionOriginGroups = model.distributionOriginGroups || [];
            model.bedTypes = model.bedTypes || [];
            loadedEnterpriseId = model.enterpriseId;
            render();
            setDirty(false);
            status.textContent = `Saved ${model.hotelName}`;
        }
        catch (error) { showError(error, "Saving property settings"); }
        finally { setBusy(false); }
    }
    function setDirty(value){dirty=value;dirtyState.textContent=value?"Unsaved changes":"No unsaved changes";dirtyState.classList.toggle("is-dirty",value)}
    function setBusy(value){save.disabled=value;hotel.disabled=value;document.querySelector(".settings-workspace").setAttribute("aria-busy",String(value))}
    function showError(error, context) {
        const detail = (error && error.message) || String(error) || "Unknown error.";
        errorPanel.replaceChildren();
        const heading = document.createElement("strong");
        heading.textContent = context ? `${context} failed` : "Something went wrong";
        const body = document.createElement("span");
        body.textContent = detail;
        errorPanel.append(heading, body);
        errorPanel.hidden = false;
        // This panel used to sit at the very bottom of the page, below the whole
        // editor, so the actual message was routinely missed and only the status
        // line was seen.
        errorPanel.scrollIntoView({block: "nearest", behavior: "smooth"});
        status.textContent = context
            ? `${context} failed - see the message above.`
            : "Something went wrong - see the message above.";
        console.error(context || "Cost Input error", error);
    }
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
        catch (error) { showError(error, "Cost data import"); }
        finally { importButton.disabled=false; }
    }
    importButton.onclick=runImport;
    document.querySelectorAll(".settings-nav button").forEach(button => {
        button.onclick = () => showSection(button.dataset.section);
    });
    document.querySelectorAll("[data-add]").forEach(button=>button.onclick=()=>{const key=button.dataset.add;model[key].push(structuredClone(defaults[key]));renderRows(key);setDirty(true);syncSectionSwitches()});
    document.querySelector("[data-add-bed-type]").onclick = () => {
        bedTypeList().push({bedName: "", linenCost: 0});
        renderCleaning();
        setDirty(true);
    };
    document.querySelector("[data-add-origin-group]").onclick = () => {
        model.distributionOriginGroups.push(newOriginGroup());
        renderDistributionTree();
        setDirty(true);
    };
    // The two switches and the franchise basis all change which controls are
    // live, so they re-sync before the generic dirty handler runs.
    form.addEventListener("change", (event) => {
        if (!event.target.name) return;
        if (CHECKBOX_FIELDS.has(event.target.name) || event.target.name === "franchiseBasis") {
            syncSectionSwitches();
        }
    });
    hotel.onchange=()=>{if(dirty&&!confirm("Discard unsaved changes?")){hotel.value=loadedEnterpriseId;return}loadSettings(hotel.value)};form.addEventListener("input",()=>setDirty(true));form.onsubmit=submit;window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue=""}});loadHotels();
}());
