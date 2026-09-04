package dev.acserver.acswitcher;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import org.bukkit.Bukkit;
import org.bukkit.configuration.ConfigurationSection;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Registry of known anticheats from config.yml. An AC is "installed" when its
 * Bukkit plugin is present and enabled. Toggling per player = granting/removing
 * the AC's bypass permission, so ANY AC with a bypass permission node works.
 */
public final class ACRegistry {

    public static final String VANILLA = "Vanilla";

    public static final class AcDef {
        public final String id;              // config key, e.g. "Intave"
        public final String pluginName;      // Bukkit plugin name
        public final String bypassPermission;// permission that disables the AC for a player

        AcDef(String id, String pluginName, String bypassPermission) {
            this.id = id;
            this.pluginName = pluginName;
            this.bypassPermission = bypassPermission;
        }

        public boolean installed() {
            org.bukkit.plugin.Plugin p = Bukkit.getPluginManager().getPlugin(pluginName);
            return p != null && p.isEnabled();
        }
    }

    private final List<AcDef> defs = new ArrayList<AcDef>();

    public ACRegistry(JavaPlugin plugin) {
        ConfigurationSection s = plugin.getConfig().getConfigurationSection("anticheats");
        if (s == null) return;
        for (String key : s.getKeys(false)) {
            String pluginName = s.getString(key + ".plugin", key);
            String bypass = s.getString(key + ".bypass-permission", "");
            if (bypass.isEmpty()) {
                plugin.getLogger().warning("Anticheat '" + key + "' in config has no bypass-permission - skipped.");
                continue;
            }
            defs.add(new AcDef(key, pluginName, bypass));
        }
    }

    public List<AcDef> all() {
        return defs;
    }

    public List<AcDef> installed() {
        List<AcDef> out = new ArrayList<AcDef>();
        for (AcDef d : defs) if (d.installed()) out.add(d);
        return out;
    }

    public AcDef byName(String name) {
        for (AcDef d : defs) if (d.id.equalsIgnoreCase(name)) return d;
        return null;
    }

    public String names() {
        StringBuilder sb = new StringBuilder();
        for (AcDef d : defs) sb.append(sb.length() == 0 ? "" : ", ").append(d.id);
        return sb.toString();
    }
}