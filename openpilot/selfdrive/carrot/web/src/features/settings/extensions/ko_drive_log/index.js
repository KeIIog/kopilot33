import { mountKoDriveLogPanel } from "./panel.js";

export function registerKoDriveLogSettingsExtension(registry) {
  return registry.register({
    id: "ko-drive-log",
    matches(context) {
      return context.group === "VEH_AUX" && !context.detailMode && Boolean(context.root);
    },
    mount(context) {
      return mountKoDriveLogPanel({ root: context.root, lifecycle: context.lifecycle });
    },
  });
}
