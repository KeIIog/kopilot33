import { mountKoDoorPanel } from "./panel.js";

export function registerKoDoorSettingsExtension(registry) {
  return registry.register({
    id: "ko-door-control",
    matches(context) {
      return context.group === "VEH_AUX" && !context.detailMode && Boolean(context.root);
    },
    mount(context) {
      return mountKoDoorPanel({ root: context.root, lifecycle: context.lifecycle });
    },
  });
}
