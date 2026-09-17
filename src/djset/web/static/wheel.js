/* Camelot wheel + key colouring, layered on top of app.js.
 *
 * Kept in its own file so the interface can carry Mixed In Key's visual
 * language without app.js having to know about drawing. It watches the result
 * table rather than forking the render path. Naming is deliberately NOT here:
 * app.js derives the placeholder from state, and a second writer reading the
 * DOM raced it into showing the previous selection's name.
 *
 * Neighbours on the wheel are neighbours in hue: a +1 move looks like a small
 * colour step and a clash looks like a jump, so the colour carries harmonic
 * distance rather than decorating the row.
 */
(() => {
  "use strict";

  const HUE = { 1: 265, 2: 292, 3: 320, 4: 350, 5: 20, 6: 44,
                7: 68, 8: 96, 9: 140, 10: 168, 11: 196, 12: 226 };

  const parse = (code) => {
    const m = /^\s*(\d{1,2})\s*([ABab])\s*$/.exec(code || "");
    return m ? { n: +m[1], l: m[2].toUpperCase() } : null;
  };

  function colour(code, on = true) {
    const k = parse(code);
    if (!k) return on ? "#E7EAF0" : "#F2F4F7";
    const s = on ? (k.l === "A" ? 62 : 55) : 10;
    const l = on ? (k.l === "A" ? 66 : 78) : 92;
    return `hsl(${HUE[k.n]},${s}%,${l}%)`;
  }

  // --- paint the Key column and collect what the set uses -------------------

  function paintKeys() {
    const rows = [...document.querySelectorAll("#result tbody tr")];
    const used = new Set();
    for (const tr of rows) {
      const cell = tr.children[4];
      if (!cell) continue;
      const code = (cell.textContent || "").trim().replace(/~$/, "");
      const k = parse(code);
      if (!k) continue;
      used.add(`${k.n}${k.l}`);
      if (cell.querySelector(".keychip")) continue;
      const chip = document.createElement("span");
      chip.className = "keychip";
      chip.style.background = colour(code);
      chip.innerHTML = cell.innerHTML;
      cell.textContent = "";
      cell.appendChild(chip);
    }
    drawWheel(used);
  }

  // --- the wheel ------------------------------------------------------------

  const NS = "http://www.w3.org/2000/svg";
  const polar = (cx, cy, r, deg) => {
    const a = ((deg - 90) * Math.PI) / 180;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  };
  const sector = (cx, cy, r1, r2, a0, a1) => {
    const p1 = polar(cx, cy, r2, a0), p2 = polar(cx, cy, r2, a1);
    const p3 = polar(cx, cy, r1, a1), p4 = polar(cx, cy, r1, a0);
    return `M${p1[0].toFixed(2)} ${p1[1].toFixed(2)}` +
           `A${r2} ${r2} 0 0 1 ${p2[0].toFixed(2)} ${p2[1].toFixed(2)}` +
           `L${p3[0].toFixed(2)} ${p3[1].toFixed(2)}` +
           `A${r1} ${r1} 0 0 0 ${p4[0].toFixed(2)} ${p4[1].toFixed(2)}Z`;
  };

  function drawWheel(used = new Set()) {
    const svg = document.getElementById("wheel");
    if (!svg) return;
    svg.textContent = "";
    const lit = used.size > 0;

    for (const [letter, r1, r2] of [["B", 94, 124], ["A", 58, 90]]) {
      for (let n = 1; n <= 12; n++) {
        const code = `${n}${letter}`;
        const on = !lit || used.has(code);
        const p = document.createElementNS(NS, "path");
        p.setAttribute("d", sector(130, 130, r1, r2, (n - 1) * 30 - 14.2, (n - 1) * 30 + 14.2));
        p.setAttribute("fill", colour(code, on));
        if (used.has(code)) {
          p.setAttribute("stroke", "#171B23");
          p.setAttribute("stroke-width", "1.4");
        }
        svg.appendChild(p);

        const mid = polar(130, 130, (r1 + r2) / 2, (n - 1) * 30);
        const t = document.createElementNS(NS, "text");
        t.setAttribute("class", "seg-lab");
        t.setAttribute("x", mid[0].toFixed(1));
        t.setAttribute("y", (mid[1] + 3.4).toFixed(1));
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("fill", on ? "#14181F" : "#B7BEC9");
        t.textContent = code;
        svg.appendChild(t);
      }
    }

    const note = document.getElementById("wheel-note");
    if (note) {
      note.textContent = lit
        ? `${used.size} ${used.size === 1 ? "key" : "keys"} in this set`
        : "Generate a set to light the wheel";
    }
  }

  // --- wire it up -----------------------------------------------------------

  const tbody = document.querySelector("#result tbody");
  if (tbody) {
    new MutationObserver(paintKeys).observe(tbody, { childList: true });
  }

  drawWheel();

  window.djsetWheel = { paintKeys, drawWheel, colour };
})();
