// Generates src/api/schema.ts from the checked-in openapi.yaml (the contract), reproducibly.
//
//   node scripts/generate-contract.mjs          write src/api/schema.ts
//   node scripts/generate-contract.mjs --check  fail if src/api/schema.ts is not what the contract generates
//
// The header records the SHA-256 of openapi.yaml, so a Python test can tell, without Node, whether the
// committed types were generated from the current contract (tests/test_frontend_contract.py).
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString } from "openapi-typescript";

const specUrl = new URL("../../openapi.yaml", import.meta.url);
const outUrl = new URL("../src/api/schema.ts", import.meta.url);

const spec = await readFile(specUrl);
const digest = createHash("sha256").update(spec).digest("hex");
// emptyObjectsUnknown: an object schema without listed properties (ExactAction.params) allows any
// properties in JSON Schema, so it becomes Record<string, unknown>, not Record<string, never>.
const ast = await openapiTS(spec.toString("utf8"), { alphabetize: true, exportType: true, emptyObjectsUnknown: true });
const header = [
  "// GENERATED FILE: do not edit. Source: openapi.yaml (the API contract).",
  `// openapi.yaml sha256: ${digest}`,
  "// Regenerate with: npm run contract:generate (frontend/scripts/generate-contract.mjs)",
  "",
].join("\n");
const generated = header + astToString(ast).replace(/\r\n/g, "\n");

if (process.argv.includes("--check")) {
  let current = "";
  try {
    current = (await readFile(outUrl, "utf8")).replace(/\r\n/g, "\n");
  } catch {
    // missing: reported below
  }
  if (current !== generated) {
    console.error("src/api/schema.ts is out of date with openapi.yaml: run npm run contract:generate");
    process.exit(1);
  }
  console.log("src/api/schema.ts matches openapi.yaml");
} else {
  await writeFile(outUrl, generated, "utf8");
  console.log("wrote src/api/schema.ts");
}
