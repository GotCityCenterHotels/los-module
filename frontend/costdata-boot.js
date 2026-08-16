// costdata-boot.js - starts the settings fetch before CSS and the editor bundle land.
//
// The editor cannot ask for a hotel's rulebook until it has parsed 20 KB gz of
// costdata-input.js and awaited /properties, but the hotel it will almost
// certainly open is already in localStorage and can be read synchronously. This
// runs from <head>, ahead of the stylesheet, so the request is in flight while
// the rest of the page is still downloading. costdata-input.js consumes the
// promise if it turns out to be for the hotel it wants, and ignores it
// otherwise.
//
// Deliberately ES5 and deliberately tiny: it has to parse and run before
// anything else on the page, and it must never be the reason the page fails.
(function () {
    var guess = null;
    try { guess = localStorage.getItem("costdata-input-property"); } catch (e) { guess = null; }
    if (!guess) return;
    window.__costBoot = {
        enterpriseId: guess,
        // No ?hotelName=: the settings route writes the supplied name back into
        // the mirrored properties table, so a stale cached name here would be
        // persisted over the correct one. Omitting it makes the server resolve
        // the id/name pair from the database instead.
        //
        // no-store until the route sends an explicit Cache-Control. With no
        // header the browser caches heuristically, and the editor can open on a
        // rulebook that was superseded hours ago.
        settings: fetch("/api/costdata/settings/" + encodeURIComponent(guess), {cache: "no-store"})
            // An error status still has a JSON body ({"error": ...}), so parsing
            // without checking would hand the editor an error object to render
            // as a rulebook.
            .then(function (r) { return r.ok ? r.json() : null; })
            .catch(function () { return null; })
    };
}());
