package dev.acserver.acswitcher;

import java.lang.reflect.Field;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

import org.bukkit.BanEntry;
import org.bukkit.BanList;
import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandMap;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerPreLoginEvent;
import org.bukkit.event.server.ServerCommandEvent;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Test-server punishment policy: bans are NOT enforced.
 *  1. The vanilla "ban" command is replaced with one that never bans: the player
 *     is told "BANNED" and the punishment is reset. This catches console
 *     dispatchCommand bans (how anticheats ban).
 *  2. A 1-second ticker auto-pardons any ban that appears via any other path
 *     (API bans) and notifies the player.
 *  3. Console-stdin "ban" commands (ServerCommandEvent) are cancelled too.
 *  4. Leftover bans are pardoned on login.
 */
public final class PunishmentReset implements Listener {

    private final JavaPlugin plugin;
    private final Set<String> pardonedOnLogin = new HashSet<String>();

    public PunishmentReset(JavaPlugin plugin) {
        this.plugin = plugin;
    }

    /** Replaces the vanilla "ban" command with a no-op that resets the punishment. */
    public void installBanOverride() {
        try {
            Object server = Bukkit.getServer();
            Field f = server.getClass().getDeclaredField("commandMap");
            f.setAccessible(true);
            CommandMap map = (CommandMap) f.get(server);
            Command old = map.getCommand("ban");
            if (old != null) old.unregister(map);
            map.register("ban", new BanOverrideCommand());
            plugin.getLogger().info("Installed ban override (bans reset instead of enforced).");
        } catch (Throwable t) {
            plugin.getLogger().warning("Could not install ban override: " + t);
        }
    }

    private final class BanOverrideCommand extends Command {
        BanOverrideCommand() {
            super("ban", "Ban override - resets punishment (test server)", "/ban <player>",
                    new java.util.ArrayList<String>());
        }

        @Override
        public boolean execute(CommandSender sender, String label, String[] args) {
            if (args.length < 1) return false;
            String name = args[0];
            notifyAndReset(name, "ban command");
            return true;
        }
    }

    private void notifyAndReset(String name, String source) {
        Player p = Bukkit.getPlayerExact(name);
        if (p != null) {
            p.sendMessage(ChatColor.DARK_RED + "" + ChatColor.BOLD + "BANNED");
            p.sendMessage(ChatColor.GRAY + "You got banned by the anticheat, but your punishment was reset - this is a test server.");
        }
        Bukkit.getBanList(BanList.Type.NAME).pardon(name);
        // in case the ban source re-bans shortly after (e.g. command chain), pardon again
        Bukkit.getScheduler().runTaskLater(plugin, () -> Bukkit.getBanList(BanList.Type.NAME).pardon(name), 20L);
        plugin.getLogger().info("Ban intercepted (" + source + ") for '" + name + "' - punishment reset (test server).");
    }

    /** Safety net: any ban entry that still appears gets pardoned within 1s. */
    public void startPardonTicker() {
        Bukkit.getScheduler().runTaskTimer(plugin, () -> {
            for (BanEntry entry : Bukkit.getBanList(BanList.Type.NAME).getBanEntries()) {
                String target = entry.getTarget();
                Player p = Bukkit.getPlayerExact(target);
                if (p != null) {
                    p.sendMessage(ChatColor.DARK_RED + "" + ChatColor.BOLD + "BANNED");
                    p.sendMessage(ChatColor.GRAY + "You got banned by the anticheat, but your punishment was reset - this is a test server.");
                }
                Bukkit.getBanList(BanList.Type.NAME).pardon(target);
                plugin.getLogger().info("Auto-pardoned ban of '" + target + "' (reason: " + entry.getReason() + ").");
            }
        }, 20L, 20L);
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onServerCommand(ServerCommandEvent e) {
        String cmd = e.getCommand() == null ? "" : e.getCommand().trim();
        if (cmd.startsWith("/")) cmd = cmd.substring(1);
        String lower = cmd.toLowerCase(Locale.ROOT);
        if (!lower.startsWith("ban ")) return;
        String[] parts = cmd.split("\\s+");
        if (parts.length < 2) return;
        e.setCancelled(true);
        notifyAndReset(parts[1], "console command");
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void onPreLogin(PlayerPreLoginEvent e) {
        String name = e.getName();
        if (Bukkit.getBanList(BanList.Type.NAME).isBanned(name)) {
            Bukkit.getBanList(BanList.Type.NAME).pardon(name);
            pardonedOnLogin.add(name.toLowerCase(Locale.ROOT));
            plugin.getLogger().info("Pardoned leftover ban for '" + name + "' on login (test server).");
        }
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent e) {
        String name = e.getPlayer().getName().toLowerCase(Locale.ROOT);
        if (pardonedOnLogin.remove(name)) {
            e.getPlayer().sendMessage(ChatColor.GRAY + "You had a ban on record, but it was reset - this is a test server.");
        }
    }
}