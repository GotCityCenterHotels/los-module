(function initializeLosPeriodPicker(root) {
    "use strict";

    const MONTHS = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
    ];

    function create({ rootElement, startInput, endInput }) {
        const selectedMonths = new Set();
        let displayYear = Number((startInput.value || new Date().getFullYear()).slice(0, 4));

        rootElement.classList.add("month-picker");
        rootElement.innerHTML = `
            <button class="month-picker-toggle" type="button" aria-expanded="false">
                Select full months
            </button>
            <div class="month-picker-menu" hidden>
                <div class="month-picker-header">
                    <button class="month-year-nav previous" type="button" aria-label="Previous year">&larr;</button>
                    <strong class="month-picker-year"></strong>
                    <button class="month-year-nav next" type="button" aria-label="Next year">&rarr;</button>
                </div>
                <div class="month-picker-grid"></div>
                <p class="month-picker-hint">Click a month to include or exclude all of its arrival dates.</p>
            </div>`;

        const toggle = rootElement.querySelector(".month-picker-toggle");
        const menu = rootElement.querySelector(".month-picker-menu");
        const yearLabel = rootElement.querySelector(".month-picker-year");
        const grid = rootElement.querySelector(".month-picker-grid");

        function monthKey(year, monthIndex) {
            return `${year}-${String(monthIndex + 1).padStart(2, "0")}`;
        }

        function lastDayOfMonth(key) {
            const [year, month] = key.split("-").map(Number);
            return new Date(Date.UTC(year, month, 0)).getUTCDate();
        }

        function notifyChange() {
            rootElement.dispatchEvent(new CustomEvent("periodchange", {
                bubbles: true,
                detail: { selectedMonths: getSelectedMonths() }
            }));
        }

        function syncDates() {
            const selected = getSelectedMonths();
            if (selected.length === 0) {
                startInput.value = "";
                endInput.value = "";
            }
            else {
                const first = selected[0];
                const last = selected[selected.length - 1];
                startInput.value = `${first}-01`;
                endInput.value = `${last}-${String(lastDayOfMonth(last)).padStart(2, "0")}`;
            }
        }

        function updateToggleText() {
            const count = selectedMonths.size;
            toggle.textContent = count === 0
                ? "Select full months"
                : `${count} full month${count === 1 ? "" : "s"} selected`;
        }

        function render() {
            yearLabel.textContent = displayYear;
            grid.innerHTML = "";

            MONTHS.forEach((label, monthIndex) => {
                const key = monthKey(displayYear, monthIndex);
                const button = document.createElement("button");
                button.type = "button";
                button.className = "month-option";
                button.textContent = label;
                button.dataset.month = key;
                button.setAttribute("aria-pressed", String(selectedMonths.has(key)));
                if (selectedMonths.has(key)) button.classList.add("selected");
                button.addEventListener("click", () => {
                    if (selectedMonths.has(key)) selectedMonths.delete(key);
                    else selectedMonths.add(key);
                    syncDates();
                    updateToggleText();
                    render();
                    notifyChange();
                });
                grid.appendChild(button);
            });
        }

        function setOpen(open) {
            menu.hidden = !open;
            toggle.setAttribute("aria-expanded", String(open));
        }

        function clearMonthSelection() {
            if (selectedMonths.size === 0) return;
            selectedMonths.clear();
            updateToggleText();
            render();
        }

        function getSelectedMonths() {
            return Array.from(selectedMonths).sort();
        }

        toggle.addEventListener("click", () => setOpen(menu.hidden));
        rootElement.querySelector(".previous").addEventListener("click", () => {
            displayYear -= 1;
            render();
        });
        rootElement.querySelector(".next").addEventListener("click", () => {
            displayYear += 1;
            render();
        });

        [startInput, endInput].forEach((input) => {
            input.addEventListener("input", clearMonthSelection);
        });
        document.addEventListener("click", (event) => {
            if (!rootElement.contains(event.target)) setOpen(false);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !menu.hidden) {
                setOpen(false);
                toggle.focus();
            }
        });

        updateToggleText();
        render();

        return { getSelectedMonths, clearMonthSelection };
    }

    root.LosPeriodPicker = { create };
}(typeof globalThis !== "undefined" ? globalThis : this));
