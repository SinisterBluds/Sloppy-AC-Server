package dev.acserver.acswitcher.flags;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedDeque;

import org.bukkit.plugin.java.JavaPlugin;

/**
 * Captured anticheat flags, per player: in-memory ring buffer + append-only
 * logs/<player>.log. Flags keep the raw line so nothing is lost.
 */
public final class FlagLog {

    public static final class Flag {
        public final long time;
        public final String player; // owner name, or null when unattributed
        public final String ac;
        public final String raw;    // original line incl. colour codes
        public final String detail; // colour-stripped line

        Flag(long time, String player, String ac, String raw, String detail) {
            this.time = time;
            this.player = player;
            this.ac = ac;
            this.raw = raw;
            this.detail = detail;
        }
    }

    private final JavaPlugin plugin;
    private final int maxPerPlayer;
    private final boolean persist;
    private final Map<String, Deque<Flag>> byPlayer = new HashMap<String, Deque<Flag>>();

    public FlagLog(JavaPlugin plugin) {
        this.plugin = plugin;
        this.maxPerPlayer = plugin.getConfig().getInt("flags.max-per-player", 500);
        this.persist = plugin.getConfig().getBoolean("flags.persist", true);
    }

    public synchronized void add(String player, String ac, String raw, String detail) {
        Flag f = new Flag(System.currentTimeMillis(), player, ac, raw, detail);
        String key = (player == null ? "$unknown" : player).toLowerCase(Locale.ROOT);
        Deque<Flag> q = byPlayer.get(key);
        if (q == null) {
            q = new ConcurrentLinkedDeque<Flag>();
            byPlayer.put(key, q);
        }
        q.addLast(f);
        while (q.size() > maxPerPlayer) q.pollFirst();
        if (persist && player != null) {
            File file = new File(plugin.getDataFolder(), "logs" + File.separator + key + ".log");
            if (!file.getParentFile().exists()) file.getParentFile().mkdirs();
            try (FileWriter w = new FileWriter(file, true)) {
                w.write(new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date(f.time))
                        + " [" + ac + "] " + detail + "\n");
            } catch (IOException ignored) {
            }
        }
    }

    /** Most recent flags for a player, newest first. */
    public synchronized List<Flag> recent(String player, int limit) {
        Deque<Flag> q = byPlayer.get(player.toLowerCase(Locale.ROOT));
        if (q == null) return Collections.emptyList();
        List<Flag> out = new ArrayList<Flag>(q);
        Collections.reverse(out);
        return out.subList(0, Math.min(limit, out.size()));
    }

    public synchronized int count(String player) {
        Deque<Flag> q = byPlayer.get(player.toLowerCase(Locale.ROOT));
        return q == null ? 0 : q.size();
    }
}