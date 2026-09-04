package dev.acserver.acswitcher.player;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;

import dev.acserver.acswitcher.ACRegistry;
import org.bukkit.configuration.file.YamlConfiguration;
import org.bukkit.entity.Player;
import org.bukkit.permissions.PermissionAttachment;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Per-player anticheat state (which ACs are ON) persisted as player-data/<uuid>.yml.
 * Applying the state = attaching/removing each AC's bypass permission on the player.
 * ponytail: one file per player, saved on every change - test-server scale, no caching layer needed.
 */
public final class PlayerStore {

    public static final class State {
        public final UUID uuid;
        public String name;
        public final Set<String> enabled = new TreeSet<String>(); // AC ids currently ON

        State(UUID uuid, String name) {
            this.uuid = uuid;
            this.name = name;
        }

        public boolean isEnabled(String acId) {
            return enabled.contains(acId);
        }
    }

    private final JavaPlugin plugin;
    private final ACRegistry registry;
    private final Map<UUID, State> states = new HashMap<UUID, State>();
    private final Map<UUID, List<PermissionAttachment>> attachments = new HashMap<UUID, List<PermissionAttachment>>();
    private final Set<String> knownNames = new HashSet<String>();

    public PlayerStore(JavaPlugin plugin, ACRegistry registry) {
        this.plugin = plugin;
        this.registry = registry;
        File dir = dataDir();
        if (dir.exists()) {
            File[] files = dir.listFiles();
            if (files != null) for (File f : files) {
                if (!f.getName().endsWith(".yml")) continue;
                String n = YamlConfiguration.loadConfiguration(f).getString("name");
                if (n != null) knownNames.add(n.toLowerCase(Locale.ROOT));
            }
        }
    }

    private File dataDir() {
        return new File(plugin.getDataFolder(), "player-data");
    }

    public ACRegistry registry() {
        return registry;
    }

    /** Loaded state for an online player (creates with defaults if unknown). */
    public State get(Player p) {
        return get(p.getUniqueId(), p.getName(), true);
    }

    public State get(UUID uuid, String name, boolean create) {
        State s = states.get(uuid);
        if (s == null) s = load(uuid, name);
        if (s == null && create) {
            s = new State(uuid, name);
            defaults(s);
            states.put(uuid, s);
        }
        if (s != null && !s.name.isEmpty()) knownNames.add(s.name.toLowerCase(Locale.ROOT));
        return s;
    }

    /** State for an offline (previously seen) player, or null. */
    public State offline(String name) {
        for (State s : states.values()) if (s.name.equalsIgnoreCase(name)) return s;
        File dir = dataDir();
        if (dir.exists()) {
            File[] files = dir.listFiles();
            if (files != null) for (File f : files) {
                if (!f.getName().endsWith(".yml")) continue;
                YamlConfiguration c = YamlConfiguration.loadConfiguration(f);
                if (name.equalsIgnoreCase(c.getString("name", ""))) {
                    try {
                        return get(UUID.fromString(f.getName().replace(".yml", "")), name, false);
                    } catch (IllegalArgumentException ignored) {
                    }
                }
            }
        }
        return null;
    }

    private void defaults(State s) {
        if (plugin.getConfig().getBoolean("default-all-on", true)) {
            for (ACRegistry.AcDef d : registry.all()) s.enabled.add(d.id);
        }
    }

    private State load(UUID uuid, String name) {
        File f = new File(dataDir(), uuid + ".yml");
        if (!f.exists()) return null;
        YamlConfiguration c = YamlConfiguration.loadConfiguration(f);
        State s = new State(uuid, c.getString("name", name == null ? "" : name));
        for (String id : c.getStringList("anticheats")) s.enabled.add(id);
        states.put(uuid, s);
        return s;
    }

    public void save(State s) {
        dataDir().mkdirs();
        YamlConfiguration c = new YamlConfiguration();
        c.set("name", s.name);
        c.set("anticheats", new ArrayList<String>(s.enabled));
        try {
            c.save(new File(dataDir(), s.uuid + ".yml"));
        } catch (IOException e) {
            plugin.getLogger().warning("Could not save state for " + s.name + ": " + e.getMessage());
        }
    }

    /**
     * Turn an AC on/off for a player (online or offline). Persists and, when the
     * player is online, immediately re-applies their permissions.
     */
    public boolean setEnabled(Player online, UUID uuid, String name, ACRegistry.AcDef ac, boolean on) {
        State s = get(uuid, name, true);
        boolean changed = on ? s.enabled.add(ac.id) : s.enabled.remove(ac.id);
        save(s);
        if (online != null) apply(online);
        return changed;
    }

    /** Reconcile bypass attachments: AC OFF for the player -> grant its bypass permission. */
    public void apply(Player p) {
        UUID uuid = p.getUniqueId();
        List<PermissionAttachment> old = attachments.remove(uuid);
        if (old != null) for (PermissionAttachment a : old) p.removeAttachment(a);
        State s = get(p);
        List<PermissionAttachment> fresh = new ArrayList<PermissionAttachment>();
        for (ACRegistry.AcDef d : registry.all()) {
            boolean grant = !s.isEnabled(d.id);
            try {
                fresh.add(p.addAttachment(plugin, d.bypassPermission, grant));
            } catch (IllegalArgumentException e) {
                // permission already attached via a wildcard node; skip
            }
        }
        for (String perm : plugin.getConfig().getStringList("grant-to-all")) {
            if (perm.isEmpty()) continue;
            try {
                fresh.add(p.addAttachment(plugin, perm, true));
            } catch (IllegalArgumentException ignored) {
            }
        }
        attachments.put(uuid, fresh);
        pushIntaveTrust(p, s);
        subscribeIntaveVerbose(p);
    }

    /**
     * Intave delivers flag chat messages only to players subscribed to its
     * VIOLATION_FINE channel (the /intave verbose self-toggle does this).
     * Subscribe every player via reflection so they see flags in chat like
     * on tree.ac - no source fork needed.
     */
    private void subscribeIntaveVerbose(Player p) {
        try {
            Class<?> subs = Class.forName("de.jpx3.intave.user.MessageChannelSubscriptions");
            Class<?> channel = Class.forName("de.jpx3.intave.user.MessageChannel");
            // VIOLATION_FINE = detailed movement data (dx/dy/dz, VL)
            Object fine = Enum.valueOf((Class<? extends Enum>) channel, "VIOLATION_FINE");
            subs.getMethod("setChannelActivation", Player.class, channel, boolean.class)
                    .invoke(null, p, fine, true);
            // NOTIFY = threshold notification messages (the sendnotify lines)
            Object notify = Enum.valueOf((Class<? extends Enum>) channel, "NOTIFY");
            subs.getMethod("setChannelActivation", Player.class, channel, boolean.class)
                    .invoke(null, p, notify, true);
        } catch (Throwable t) {
            plugin.getLogger().fine("Intave channel subscription skipped for " + p.getName() + ": " + t);
        }
    }

    /**
     * Intave resolves trust only on join, so a live permission change would only
     * apply after rejoin. Push the trust factor directly through Intave's API via
     * reflection (no compile dependency) so toggles apply instantly.
     * ponytail: reflection + try/catch - if Intave is absent or the API moves, nothing happens.
     */
    private void pushIntaveTrust(Player p, State s) {
        try {
            Class<?> pluginClass = Class.forName("de.jpx3.intave.IntavePlugin");
            Object instance = pluginClass.getMethod("singletonInstance").invoke(null);
            if (instance == null) return;
            Object access = pluginClass.getMethod("access").invoke(instance);
            if (access == null) return;
            // invoke through the public interfaces - the impl is an anonymous class
            Class<?> accessIface = Class.forName("de.jpx3.intave.access.IntaveAccess");
            Object playerAccess = accessIface.getMethod("player", Player.class).invoke(access, p);
            if (playerAccess == null) return;
            Class<?> tf = Class.forName("de.jpx3.intave.access.player.trust.TrustFactor");
            boolean bypass = !s.isEnabled("Intave");
            Object factor = Enum.valueOf((Class<? extends Enum>) tf, bypass ? "BYPASS" : "ORANGE");
            Class<?> paIface = Class.forName("de.jpx3.intave.access.player.PlayerAccess");
            paIface.getMethod("setTrustFactor", tf).invoke(playerAccess, factor);
            plugin.getLogger().info("Pushed Intave trust factor " + factor + " for " + p.getName());
        } catch (Throwable t) {
            // Intave not present, or API changed - permission attachment alone still works on rejoin
            plugin.getLogger().info("Intave trust push skipped for " + p.getName() + ": " + t);
        }
    }

    public void clear(Player p) {
        List<PermissionAttachment> old = attachments.remove(p.getUniqueId());
        if (old != null) for (PermissionAttachment a : old) p.removeAttachment(a);
    }

    public Set<String> knownNames() {
        return knownNames;
    }

    public void noteName(String name) {
        knownNames.add(name.toLowerCase(Locale.ROOT));
    }
}