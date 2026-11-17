import { build } from "esbuild";

await build({
  entryPoints: ["frontend/admin/index.ts"],
  outbase: "frontend",
  outdir: "frontend/dist",
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: true,
});
