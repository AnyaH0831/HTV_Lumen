export async function resolve(specifier, ctx, next) {
  if (specifier === "three") return { url: new URL("./three.js", import.meta.url).href, shortCircuit: true };
  return next(specifier, ctx);
}
