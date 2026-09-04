package dev.acserver.acswitcher;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;

import com.google.gson.JsonObject;

import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.Location;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.util.Vector;

/**
 * Broadcasts captured AC flags to all online players and writes per-player
 * telemetry JSON-lines for bypass development. Gives every flagged player's
 * full movement state (position, velocity, rotation, health, etc.) at the
 * moment of the flag. Only uses 1.8.8-compatible Bukkit API methods.
 */
public final class FlagTelemetry {

    private final JavaPlugin plugin;
    private final File telemetryDir;

    public FlagTelemetry(JavaPlugin plugin) {
        this.plugin = plugin;
        this.telemetryDir = new File(plugin.getDataFolder(), "telemetry");
        if (!telemetryDir.exists()) telemetryDir.mkdirs();
    }

    /**
     * Called by FlagCapture when a flag is captured.
     * Broadcasts a formatted line to all players + writes telemetry JSON.
     */
    public void onFlag(String player, String ac, String raw, String detail) {
        if (detail.contains("joined with version") || detail.contains("left the game")) return;
        if (detail.startsWith("[ACSwitcher] Emitted:")) return;

        // --- broadcast to all players with acswitcher.flags permission ---
        Player p = (player != null) ? Bukkit.getPlayerExact(player) : null;
        String broadcast = formatForChat(ac, detail);
        // Append player position/velocity data when available
        if (p != null && p.isOnline()) {
            Location loc = p.getLocation();
            Vector vel = p.getVelocity();
            broadcast += " §8| pos" + r(loc.getX()) + "," + r(loc.getY()) + "," + r(loc.getZ())
                    + " vel " + r(vel.getX()) + "," + r(vel.getY()) + "," + r(vel.getZ());
        }
        for (Player online : Players.online()) {
            if (online.hasPermission("acswitcher.flags")) {
                online.sendMessage(broadcast);
            }
        }

        // --- telemetry log: full player state at flag time ---
        if (p != null && p.isOnline()) {
            writeTelemetry(ac, player, detail, p);
        }
    }

    private static String r(double v) {
        return String.format("%.2f", v);
    }

    private String formatForChat(String ac, String detail) {
        String cleaned = detail;

        // Strip "[ACSwitcher] " prefix from console capture
        cleaned = cleaned.replaceAll("^\\[ACSwitcher\\]\\s*", "");

        // Strip "Issued server command /intave internals sendnotify " noise
        int sendnotifyIdx = cleaned.indexOf("sendnotify");
        if (sendnotifyIdx > 0) {
            int start = sendnotifyIdx + "sendnotify".length();
            if (start < cleaned.length()) {
                cleaned = cleaned.substring(start).trim();
                cleaned = cleaned.replaceAll("^&[0-9a-fk-or]", "");
            }
        }

        // If already prefixed with [Intave] or similar, use as-is with colors
        if (cleaned.startsWith("[") || cleaned.startsWith("§")) {
            return ChatColor.translateAlternateColorCodes('&', cleaned);
        }

        // Otherwise add our own prefix
        String prefix = acColor(ac) + " ";
        return ChatColor.translateAlternateColorCodes('&', prefix + cleaned);
    }

    private String acColor(String ac) {
        if (ac.equalsIgnoreCase("Intave")) return "&8[&c&lIntave&8]";
        if (ac.equalsIgnoreCase("Grim") || ac.equalsIgnoreCase("GrimAC")) return "&bGrim &8\u00BB";
        if (ac.equalsIgnoreCase("Spartan")) return "&8[&2Spartan&8]";
        return "&8[" + ac + "]";
    }

    /**
     * Write a telemetry entry (JSON line) with full player state at flag time.
     * This is the key output for bypass development — position, velocity,
     * rotation, health, food, sprinting state, and the flag data.
     * Only uses 1.8.8-compatible Bukkit API methods.
     */
    private void writeTelemetry(String ac, String player, String detail, Player p) {
        try {
            JsonObject entry = new JsonObject();
            entry.addProperty("time", System.currentTimeMillis());
            entry.addProperty("ac", ac);
            entry.addProperty("player", player);
            entry.addProperty("flag", detail);

            // Position
            Location loc = p.getLocation();
            entry.add("position", jsonVec(loc.getX(), loc.getY(), loc.getZ()));

            // Velocity
            Vector vel = p.getVelocity();
            entry.add("velocity", jsonVec(vel.getX(), vel.getY(), vel.getZ()));

            // Rotation
            JsonObject rot = new JsonObject();
            rot.addProperty("yaw", round(loc.getYaw()));
            rot.addProperty("pitch", round(loc.getPitch()));
            entry.add("rotation", rot);

            // Movement state
            entry.addProperty("onGround", p.isOnGround());
            entry.addProperty("fallDistance", round(p.getFallDistance()));
            entry.addProperty("ticksLived", p.getTicksLived());

            // Combat
            entry.addProperty("health", round(p.getHealth()));
            entry.addProperty("food", p.getFoodLevel());
            entry.addProperty("saturatedFood", round(p.getSaturation()));
            entry.addProperty("isSprinting", p.isSprinting());
            entry.addProperty("isSneaking", p.isSneaking());

            // Network / client
            entry.addProperty("gamemode", p.getGameMode().name());
            entry.addProperty("heldSlot", p.getInventory().getHeldItemSlot());

            // Write JSON line
            File logFile = new File(telemetryDir, player.toLowerCase() + ".jsonl");
            try (FileWriter fw = new FileWriter(logFile, true)) {
                fw.write(entry.toString() + "\n");
            }
        } catch (IOException | IllegalArgumentException e) {
            plugin.getLogger().fine("Telemetry write failed for " + player + ": " + e.getMessage());
        }
    }

    private static JsonObject jsonVec(double x, double y, double z) {
        JsonObject v = new JsonObject();
        v.addProperty("x", round(x));
        v.addProperty("y", round(y));
        v.addProperty("z", round(z));
        return v;
    }

    private static double round(double v) {
        return Math.round(v * 1000000.0) / 1000000.0;
    }
}