(function initializeLosFormat(root, factory) {
    const api = factory();

    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }

    root.LosFormat = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createLosFormat() {
    "use strict";

    const MONTHS = Object.freeze([
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
    ]);

    const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

    // ---------------------------------------------------------------------
    // SEK
    //
    // Every SEK amount in this application - displayed figure and editable
    // input alike - is whole kronor. Fractions are rounded once, here, so a
    // column of rounded rows still adds up to its rounded total instead of
    // drifting by a few ore per row.
    // ---------------------------------------------------------------------
    const SEK_FRACTION_DIGITS = 0;

    // The single registry of money fields. A field listed here gets the whole
    // krona input treatment wherever it is rendered; nothing else does. Hours,
    // minutes and percentages are not money and keep their decimals.
    const MONEY_FIELDS = Object.freeze(new Set([
        "cleaningCostPerMinute",
        "receptionCostPerHour",
        "breakfastFoodCostPerGuest",
        "breakfastStaffCostPerHour",
        "linenCost"
    ]));

    const sekFormatter = new Intl.NumberFormat("en-SE", {
        minimumFractionDigits: SEK_FRACTION_DIGITS,
        maximumFractionDigits: SEK_FRACTION_DIGITS
    });

    function isMoneyField(field) {
        return MONEY_FIELDS.has(field);
    }

    function roundSek(value) {
        // Number(null) and Number("") are both 0, which would turn "no value"
        // into a real zero on screen and in a total.
        if (value === null || value === undefined || value === "") return null;
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return null;
        // Math.round is half-up towards +Infinity, which turns -0.5 into -0.
        // Costs and revenue can both be negative in a correction period, so
        // round the magnitude and restore the sign.
        const rounded = Math.sign(parsed) * Math.round(Math.abs(parsed));
        return rounded === 0 ? 0 : rounded;
    }

    function formatSek(value) {
        const rounded = roundSek(value);
        return rounded === null ? "—" : sekFormatter.format(rounded);
    }

    function formatSekAmount(value) {
        const rounded = roundSek(value);
        return rounded === null ? "—" : `${sekFormatter.format(rounded)} kr`;
    }

    // Input value normalisation. Returns a string so it can be assigned
    // straight back onto an <input>, and keeps "" as "" - a blank optional
    // field must not become 0.
    function normalizeSekInputValue(value) {
        if (value === "" || value === null || value === undefined) return "";
        const rounded = roundSek(value);
        return rounded === null ? String(value) : String(rounded);
    }

    // The shared SEK input component. Normalisation runs on blur rather than
    // on input so typing "1500" is not rewritten mid-keystroke and the caret
    // does not jump.
    function bindSekInput(input, onChange) {
        if (!input) return input;
        input.type = "number";
        input.step = "1";
        if (!input.min) input.min = "0";
        input.inputMode = "numeric";
        input.value = normalizeSekInputValue(input.value);
        input.addEventListener("blur", () => {
            const normalised = normalizeSekInputValue(input.value);
            if (normalised === input.value) return;
            input.value = normalised;
            if (onChange) onChange(normalised);
        });
        return input;
    }

    // ---------------------------------------------------------------------
    // Period labels
    //
    // Period keys are the first ISO date of the bucket, so a month bucket and
    // a day bucket are indistinguishable from the key alone: 2026-01-01 is
    // both "1 Jan 2026" and "Jan 2026". The grain is what decides, which is
    // why every caller must pass it.
    // ---------------------------------------------------------------------
    function parts(periodKey) {
        const match = ISO_DATE.exec(String(periodKey ?? ""));
        if (!match) return null;
        const [, year, month, day] = match;
        return {
            year,
            monthIndex: Number(month) - 1,
            day: Number(day),
            paddedDay: day
        };
    }

    // ISO-8601 week: weeks start on Monday and a week belongs to the year
    // holding its Thursday, so the last days of December can be week 1 of the
    // following year. Returning the week-year alongside the number keeps the
    // label honest at that boundary.
    function isoWeek(periodKey) {
        const value = parts(periodKey);
        if (!value) return null;
        const date = new Date(Date.UTC(
            Number(value.year), value.monthIndex, value.day
        ));
        const dayOfWeek = (date.getUTCDay() + 6) % 7;
        date.setUTCDate(date.getUTCDate() - dayOfWeek + 3);
        const weekYear = date.getUTCFullYear();
        const firstThursday = new Date(Date.UTC(weekYear, 0, 4));
        const firstDayOfWeek = (firstThursday.getUTCDay() + 6) % 7;
        firstThursday.setUTCDate(firstThursday.getUTCDate() - firstDayOfWeek + 3);
        const week = 1 + Math.round(
            (date.getTime() - firstThursday.getTime()) / (7 * 86400000)
        );
        return {week, weekYear: String(weekYear)};
    }

    // ---------------------------------------------------------------------
    // Last year
    //
    // Two bases, the same two the LOS API accepts. "Same date" is the calendar
    // date a year away - what a finance reader means by last year. "Same weekday"
    // is 364 days, which is 52 whole weeks, so a Saturday compares with a
    // Saturday; for anything driven by day of week - arrivals, departures, a
    // weekend rate - that is the only comparison that is not mostly noise.
    // ---------------------------------------------------------------------
    const LY_WEEKDAY_OFFSET_DAYS = 364;

    function isLeapYear(year) {
        return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    }

    function isoPlusDays(isoDate, days) {
        const value = parts(isoDate);
        if (!value) return String(isoDate ?? "");
        return new Date(Date.UTC(
            Number(value.year), value.monthIndex, value.day + days
        )).toISOString().slice(0, 10);
    }

    // The same calendar date, one year away, done on the string rather than
    // through Date. Date rolls 29 February onto 1 March, which would move a day
    // into the following month and, at a month grain, into the wrong bar. Here
    // both of a leap year's late-February days land on the 28th instead, so the
    // comparison keeps every krona rather than dropping a day out of one side.
    function shiftCalendarYear(isoDate, years) {
        const iso = String(isoDate ?? "").slice(0, 10);
        if (!ISO_DATE.test(iso)) return iso;
        const target = Number(iso.slice(0, 4)) + years;
        const monthDay = iso.slice(5);
        return monthDay === "02-29" && !isLeapYear(target)
            ? `${target}-02-28`
            : `${target}-${monthDay}`;
    }

    function shiftYear(isoDate, basis, direction) {
        return basis === "sameWeekday"
            ? isoPlusDays(isoDate, direction * LY_WEEKDAY_OFFSET_DAYS)
            : shiftCalendarYear(isoDate, direction);
    }

    /** The date one year before this one, on the given basis. */
    function lastYearDate(isoDate, basis) {
        return shiftYear(isoDate, basis, -1);
    }

    /**
     * The date a last-year date compares against - the inverse of lastYearDate.
     *
     * This is what lets last year's rows be restamped with the dates they compare
     * against, so both years bucket into the same periods by construction rather
     * than being paired by position.
     */
    function thisYearDate(isoDate, basis) {
        return shiftYear(isoDate, basis, 1);
    }

    /** A {startDate, endDate} range, one year back. */
    function lastYearRange(range, basis) {
        return {
            startDate: lastYearDate(range.startDate, basis),
            endDate: lastYearDate(range.endDate, basis)
        };
    }

    function periodLabel(periodKey, grain = "day") {
        const value = parts(periodKey);
        // "All" and anything that is not an ISO date is already a label.
        if (!value) return String(periodKey ?? "");

        if (grain === "year") return value.year;
        if (grain === "month") return `${MONTHS[value.monthIndex]} ${value.year}`;
        if (grain === "week") {
            const week = isoWeek(periodKey);
            return `Wk ${String(week.week).padStart(2, "0")} ${week.weekYear}`;
        }
        return `${value.day} ${MONTHS[value.monthIndex]} ${value.year}`;
    }

    // Chart axes stack the period above a year band, so they need the label
    // split. Month and year carry their own year and return none, which keeps
    // "Jan 2026" from being printed twice under the same tick.
    function periodLabelParts(periodKey, grain = "day") {
        const value = parts(periodKey);
        if (!value) return {primary: String(periodKey ?? ""), year: null};

        if (grain === "year") return {primary: value.year, year: null};
        if (grain === "month") {
            return {primary: `${MONTHS[value.monthIndex]} ${value.year}`, year: null};
        }
        if (grain === "week") {
            const week = isoWeek(periodKey);
            return {
                primary: `Wk ${String(week.week).padStart(2, "0")}`,
                year: week.weekYear
            };
        }
        return {
            primary: `${value.paddedDay} ${MONTHS[value.monthIndex]}`,
            year: value.year
        };
    }

    return {
        MONTHS,
        SEK_FRACTION_DIGITS,
        MONEY_FIELDS,
        isMoneyField,
        roundSek,
        formatSek,
        formatSekAmount,
        normalizeSekInputValue,
        bindSekInput,
        isoWeek,
        periodLabel,
        periodLabelParts,
        LY_WEEKDAY_OFFSET_DAYS,
        isoPlusDays,
        lastYearDate,
        thisYearDate,
        lastYearRange
    };
}));
