// Write the evaluation numbers from site/data/eval.json into the site's HTML.
//   node scripts/render-eval.mjs
// Any element marked data-eval="e1.avo_right" gets that number as its text, and
// data-eval-w="e3.bare_pct" sets a bar's --w width. The numbers are in the HTML
// itself, so they show without JavaScript; change eval.json, run this, and every
// page agrees.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const data = JSON.parse(readFileSync(path.join(ROOT, "site/data/eval.json"), "utf8"));
const get = (key) => {
  const v = key.split(".").reduce((o, k) => (o == null ? undefined : o[k]), data);
  if (v === undefined) throw new Error(`eval.json has no ${key}`);
  return v;
};
let total = 0;
for (const page of ["site/index.html", "site/docs.html"]) {
  const file = path.join(ROOT, page);
  let html = readFileSync(file, "utf8");
  let n = 0;
  html = html.replace(/(<([a-z0-9]+)\b[^>]*\bdata-eval="([\w.]+)"[^>]*>)([^<]*)(<\/\2>)/g, (_, open, _tag, key, _old, close) => (n++, open + get(key) + close));
  html = html.replace(/(\bdata-eval-w="([\w.]+)"[^>]*\bstyle="--w: )[\d.]+%/g, (_, head, key) => (n++, `${head}${get(key)}%`));
  writeFileSync(file, html);
  console.log(`${page}: ${n} numbers`);
  total += n;
}
if (!total) console.log("no data-eval marks found");
