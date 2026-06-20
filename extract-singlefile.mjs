import { writeFileSync } from "fs";
import { script, hookScript, zipScript } from "single-file-cli/lib/single-file-bundle.js";

writeFileSync("/app/singlefile-injected.js", script);
writeFileSync("/app/singlefile-hook.js", hookScript);
writeFileSync("/app/singlefile-zip.js", zipScript);
console.log("Extracted SingleFile scripts");
