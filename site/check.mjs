// Confirm the website's statistics reproduce the Python results exactly.
//   node site/check.mjs
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("./public/index.html", import.meta.url), "utf8");
const script = html.slice(html.lastIndexOf("<script>") + 8, html.lastIndexOf("</script>"));
const engine = script.slice(0, script.indexOf("const esc ="));
const { DATA, compute } = new Function(`${engine}; return { DATA, compute };`)();

let worst = 0, mismatches = 0, arms = 0;
const result = compute(0.10, 0.05);
for (const run of DATA.runs) {
  for (const arm of run.arms) {
    const segment = result.segments.find(s => s.name === arm.segment && s.run === run);
    if (!segment) continue;
    const mine = segment.arms.find(a => a.candidate === arm.candidate);
    arms++;
    worst = Math.max(worst, Math.abs(mine.logFor - arm.published.log_e_for),
                     Math.abs(mine.logAgainst - arm.published.log_e_against));
    if (mine.verdict !== arm.published.verdict) mismatches++;
  }
}
console.log(`${arms} arms checked, largest log-evidence difference ${worst.toExponential(2)}, ` +
            `${mismatches} verdict mismatches; guard rollback at ${DATA.guard.rollback_at}`);
process.exit(mismatches || worst > 1e-9 || !DATA.guard.rollback_at ? 1 : 0);
