package dev.acserver.acswitcher.flags;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

import dev.acserver.acswitcher.ACRegistry;
import dev.acserver.acswitcher.FlagTelemetry;
import dev.acserver.acswitcher.Players;
import dev.acserver.acswitcher.player.PlayerStore;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.Logger;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Captures anticheat flag lines from the server console by attaching an appender
 * to the root log4j2 logger. AC-agnostic: works for ANY anticheat that prints
 * flags to console with a "[AC]" prefix, no per-AC API hook needed.
 * ponytail: if log4j2 core is missing the server fails gracefully (no capture).
 */
public final class FlagCapture extends AbstractAppender {

    private final ACRegistry registry;
    private final PlayerStore store;
    private final FlagLog log;
    private final FlagTelemetry telemetry;
    private boolean ok = false;

    public FlagCapture(JavaPlugin plugin, ACRegistry registry, PlayerStore store, FlagLog log, FlagTelemetry telemetry) {
        super("ACSwitcher", null, null);
        this.registry = registry;
        this.store = store;
        this.log = log;
        this.telemetry = telemetry;
        start();
        try {
            Logger root = (Logger) LogManager.getRootLogger();
            root.addAppender(this);
            ok = true;
            plugin.getLogger().info("Flag capture attached: " + registry.all().size() + " AC prefix(es) watched.");
        } catch (Throwable t) {
            plugin.getLogger().warning("Flag capture unavailable (no log4j2 core?): " + t);
        }
    }

    /** Detach from the root logger (called on plugin disable so late shutdown
     *  messages don't hit a stopped appender). */
    public void shutdown() {
        try {
            if (ok) {
                Logger root = (Logger) LogManager.getRootLogger();
                root.removeAppender(this);
                ok = false;
            }
        } catch (Throwable ignored) {
        }
        stop();
    }

    @Override
    public void append(LogEvent event) {
        if (!ok || event.getMessage() == null) return;
        String raw = event.getMessage().getFormattedMessage();
        if (raw == null || raw.isEmpty()) return;
        String clean = ChatColor.stripColor(ChatColor.translateAlternateColorCodes('&', raw));
        String ac = matchAc(clean);
        if (ac == null) return;
        // Skip non-flag lines (join messages, trustfactor changes, system messages)
        String lower = clean.toLowerCase(Locale.ROOT);
        if (lower.contains("joined") || lower.contains("assigned") || lower.contains("changed trustfactor")
                || lower.contains("trustfactor of") || lower.contains("enabling") || lower.contains("disabling")
                || lower.contains("loading") || lower.contains("version")) return;
        String player = matchPlayer(clean);
        log.add(player, ac, raw, clean);
        if (telemetry != null) telemetry.onFlag(player, ac, raw, clean);
    }

    /** A line is a flag from AC "X" when it starts with "[X" or "X " (covers both [GrimAC] and Grim » formats). */
    private String matchAc(String clean) {
        String lower = clean.toLowerCase(Locale.ROOT);
        for (ACRegistry.AcDef d : registry.all()) {
            String idLower = d.id.toLowerCase(Locale.ROOT);
            if (lower.contains("[" + idLower) || lower.startsWith(idLower + " ")) return d.id;
        }
        return null;
    }

    private static final Pattern WORD = Pattern.compile("[A-Za-z0-9_]+");

    /** Attribute the line to the longest known player name contained in it. */
    private String matchPlayer(String clean) {
        String best = null;
        for (Player p : dev.acserver.acswitcher.Players.online()) {
            if (containsWord(clean, p.getName())) {
                if (best == null || p.getName().length() > best.length()) best = p.getName();
            }
        }
        if (best != null) return best;
        for (String name : store.knownNames()) {
            if (containsWord(clean, name)) {
                if (best == null || name.length() > best.length()) best = name;
            }
        }
        return best;
    }

    private static boolean containsWord(String text, String word) {
        if (word.isEmpty()) return false;
        List<String> words = new ArrayList<String>();
        java.util.regex.Matcher m = WORD.matcher(text);
        while (m.find()) words.add(m.group());
        String w = word.toLowerCase(Locale.ROOT);
        for (String t : words) if (t.equalsIgnoreCase(w)) return true;
        return false;
    }
}