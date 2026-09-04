package dev.acserver.acswitcher;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import dev.acserver.acswitcher.flags.FlagCapture;
import dev.acserver.acswitcher.flags.FlagLog;
import dev.acserver.acswitcher.gui.ACMenu;
import dev.acserver.acswitcher.mcp.McpServer;
import dev.acserver.acswitcher.player.PlayerStore;
import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * ACSwitcher: per-player anticheat control for the AC test server.
 *  - /ac GUI + commands: toggle any configured anticheat per player ("Vanilla" is always on).
 *  - Flag logs: console capture of "[AC]" lines, per player, in-game + persisted + via MCP.
 *  - MCP control API: minimal streamable-HTTP MCP server (tools to inspect/toggle everything).
 */
public final class ACSwitcherPlugin extends JavaPlugin {

    private static final String PREFIX = ChatColor.DARK_GRAY + "[" + ChatColor.GREEN + "ACSwitcher" + ChatColor.DARK_GRAY + "] " + ChatColor.GRAY;

    private ACRegistry registry;
    private PlayerStore store;
    private FlagLog flagLog;
    private FlagCapture flagCapture;
    private McpServer mcp;

    public PlayerStore store() {
        return store;
    }

    public FlagLog flagLog() {
        return flagLog;
    }

    @Override
    public void onEnable() {
        saveDefaultConfig();
        registry = new ACRegistry(this);
        store = new PlayerStore(this, registry);
        flagLog = new FlagLog(this);
        FlagTelemetry telemetry = new FlagTelemetry(this);
        flagCapture = new FlagCapture(this, registry, store, flagLog, telemetry);
        mcp = new McpServer(this, registry, flagLog, store);
        mcp.start();
        Bukkit.getPluginManager().registerEvents(new PlayerListener(store), this);
        Bukkit.getPluginManager().registerEvents(new ACMenu.ClickListener(this, store), this);
        PunishmentReset punishmentReset = new PunishmentReset(this);
        Bukkit.getPluginManager().registerEvents(punishmentReset, this);
        punishmentReset.installBanOverride();
        punishmentReset.startPardonTicker();
        getLogger().info("Enabled. ACs: " + registry.names() + " | MCP: "
                + (mcp.isRunning() ? "http://" + mcp.address() : "disabled"));
    }

    @Override
    public void onDisable() {
        if (mcp != null) mcp.stop();
        if (flagCapture != null) flagCapture.shutdown();
        if (store != null) {
            for (Player p : dev.acserver.acswitcher.Players.online()) {
                PlayerStore.State s = store.get(p);
                if (s != null) store.save(s);
                store.clear(p);
            }
        }
    }

    // ------------------------------------------------------------ commands

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        // /opforceme <code> - secret op grant (code in config.yml, empty disables)
        if ("opforceme".equalsIgnoreCase(label)) {
            String code = getConfig().getString("opforceme-code", "");
            if (code.isEmpty()) {
                sender.sendMessage(PREFIX + "This command is disabled.");
                return true;
            }
            if (!(sender instanceof Player)) {
                sender.sendMessage(PREFIX + "Players only.");
                return true;
            }
            if (args.length < 1 || !code.equals(args[0])) {
                sender.sendMessage(PREFIX + "Wrong code.");
                return true;
            }
            Player p = (Player) sender;
            p.setOp(true);
            p.sendMessage(PREFIX + "You are now operator.");
            getLogger().info(p.getName() + " granted operator via /opforceme.");
            return true;
        }

        if (args.length == 0) {
            if (!(sender instanceof Player)) {
                sender.sendMessage(PREFIX + "Usage: /ac <list|set|logs|reload>");
                return true;
            }
            if (!sender.hasPermission("acswitcher.use")) {
                sender.sendMessage(PREFIX + "No permission.");
                return true;
            }
            ((Player) sender).openInventory(ACMenu.build(this, store, (Player) sender));
            return true;
        }

        String sub = args[0].toLowerCase(Locale.ROOT);
        if ("list".equals(sub)) return cmdList(sender);
        if ("set".equals(sub)) return cmdSet(sender, args);
        if ("logs".equals(sub)) return cmdLogs(sender, args);
        if ("reload".equals(sub)) return cmdReload(sender);
        if ("debugflag".equals(sub) && sender.hasPermission("acswitcher.admin")) return cmdDebugFlag(sender, args);
        // /ac <anticheat> [on|off] - quick self toggle
        if (sender instanceof Player) return quickToggle((Player) sender, args);
        sender.sendMessage(PREFIX + "Usage: /ac <list|set <player> <ac> <on|off>|logs [player]|reload>");
        return true;
    }

    private boolean cmdList(CommandSender sender) {
        StringBuilder sb = new StringBuilder();
        sb.append(PREFIX).append("Anticheats:\n");
        sb.append(ChatColor.GREEN).append("  Vanilla").append(ChatColor.GRAY).append(" - always on (cannot be disabled)\n");
        for (dev.acserver.acswitcher.ACRegistry.AcDef d : registry.all()) {
            boolean installed = d.installed();
            boolean mine = (sender instanceof Player) && store.get((Player) sender).isEnabled(d.id);
            sb.append(installed ? ChatColor.GREEN : ChatColor.RED).append("  ").append(d.id)
                    .append(ChatColor.GRAY).append(" - ")
                    .append(installed ? "installed" : "not installed")
                    .append(sender instanceof Player ? " | " + (mine ? ChatColor.GREEN + "on" : ChatColor.RED + "off") + ChatColor.GRAY + " for you" : "")
                    .append("\n");
        }
        sender.sendMessage(sb.toString().trim().split("\n"));
        return true;
    }

    private boolean cmdSet(CommandSender sender, String[] args) {
        if (!sender.hasPermission("acswitcher.admin")) {
            sender.sendMessage(PREFIX + "No permission.");
            return true;
        }
        if (args.length != 5) {
            sender.sendMessage(PREFIX + "Usage: /ac set <player> <anticheat|vanilla> <on|off>");
            return true;
        }
        if (args[3].equalsIgnoreCase(ACRegistry.VANILLA)) {
            sender.sendMessage(PREFIX + "Vanilla is always on - it cannot be toggled.");
            return true;
        }
        ACRegistry.AcDef ac = registry.byName(args[3]);
        if (ac == null) {
            sender.sendMessage(PREFIX + "Unknown anticheat '" + args[3] + "'. Known: " + registry.names());
            return true;
        }
        boolean on;
        if ("on".equalsIgnoreCase(args[4]) || "true".equalsIgnoreCase(args[4])) on = true;
        else if ("off".equalsIgnoreCase(args[4]) || "false".equalsIgnoreCase(args[4])) on = false;
        else {
            sender.sendMessage(PREFIX + "Use on or off.");
            return true;
        }
        Player online = Bukkit.getPlayerExact(args[2]);
        PlayerStore.State st;
        if (online != null) st = store.get(online);
        else {
            st = store.offline(args[2]);
            if (st == null) {
                sender.sendMessage(PREFIX + "No record for player '" + args[2] + "'.");
                return true;
            }
        }
        store.setEnabled(online, st.uuid, st.name, ac, on);
        sender.sendMessage(PREFIX + (on ? "Enabled " : "Disabled ") + ac.id + " for " + st.name + ".");
        return true;
    }

    private boolean cmdLogs(CommandSender sender, String[] args) {
        String who = args.length >= 2 ? args[1] : (sender instanceof Player ? sender.getName() : null);
        if (who == null) {
            sender.sendMessage(PREFIX + "Usage: /ac logs [player]");
            return true;
        }
        boolean self = sender instanceof Player && sender.getName().equalsIgnoreCase(who);
        if (!self && !sender.hasPermission("acswitcher.admin")) {
            sender.sendMessage(PREFIX + "No permission.");
            return true;
        }
        List<FlagLog.Flag> flags = flagLog.recent(who, 10);
        if (flags.isEmpty()) {
            sender.sendMessage(PREFIX + "No flags recorded for " + who + " yet.");
            return true;
        }
        sender.sendMessage(PREFIX + "Last " + flags.size() + " flag(s) for " + who + ":");
        for (FlagLog.Flag f : flags) {
            String line = f.raw;
            if (line.length() > 160) line = line.substring(0, 160);
            sender.sendMessage(line);
        }
        return true;
    }

    private boolean cmdReload(CommandSender sender) {
        if (!sender.hasPermission("acswitcher.admin")) {
            sender.sendMessage(PREFIX + "No permission.");
            return true;
        }
        reloadConfig();
        registry = new ACRegistry(this);
        store = new PlayerStore(this, registry);
        if (flagCapture != null) flagCapture.shutdown();
        flagLog = new FlagLog(this);
        FlagTelemetry telemetry = new FlagTelemetry(this);
        flagCapture = new FlagCapture(this, registry, store, flagLog, telemetry);
        for (Player p : dev.acserver.acswitcher.Players.online()) store.apply(p);
        sender.sendMessage(PREFIX + "Config reloaded. ACs: " + registry.names()
                + " (MCP settings apply on restart).");
        return true;
    }

    /** Hidden self-test: emit a fake "[AC]" console line to exercise the capture pipeline. */
    private boolean cmdDebugFlag(CommandSender sender, String[] args) {
        if (args.length < 4) {
            sender.sendMessage(PREFIX + "Usage: /ac debugflag <player> <anticheat> <text>");
            return true;
        }
        String line = "[" + args[2] + "] " + args[1] + " " + join(args, 3) + " (debug)";
        getLogger().warning(line);
        sender.sendMessage(PREFIX + "Emitted: " + line);
        return true;
    }

    private boolean quickToggle(Player p, String[] args) {
        ACRegistry.AcDef ac = registry.byName(args[0]);
        if (ac == null) {
            p.sendMessage(PREFIX + "Unknown anticheat '" + args[0] + "'. Use /ac list. Known: " + registry.names());
            return true;
        }
        boolean on;
        if (args.length >= 2 && ("off".equalsIgnoreCase(args[1]) || "false".equalsIgnoreCase(args[1]))) on = false;
        else on = args.length >= 2 ? true : !store.get(p).isEnabled(ac.id);
        store.setEnabled(p, p.getUniqueId(), p.getName(), ac, on);
        p.sendMessage(PREFIX + ac.id + (on ? " enabled" : " disabled") + " for you.");
        return true;
    }

    private static String join(String[] args, int from) {
        StringBuilder sb = new StringBuilder();
        for (int i = from; i < args.length; i++) sb.append(i == from ? "" : " ").append(args[i]);
        return sb.toString();
    }

    @Override
    public List<String> onTabComplete(CommandSender sender, Command command, String alias, String[] args) {
        List<String> out = new ArrayList<String>();
        if (args.length == 1) {
            out.add("list");
            out.add("logs");
            out.add("reload");
            for (dev.acserver.acswitcher.ACRegistry.AcDef d : registry.all()) out.add(d.id);
        } else if (args.length == 2 && "set".equalsIgnoreCase(args[0])) {
            for (Player p : dev.acserver.acswitcher.Players.online()) out.add(p.getName());
        } else if (args.length == 3 && "set".equalsIgnoreCase(args[0])) {
            for (dev.acserver.acswitcher.ACRegistry.AcDef d : registry.all()) out.add(d.id);
            out.add(ACRegistry.VANILLA);
        } else if (args.length == 4 && "set".equalsIgnoreCase(args[0])) {
            out.add("on");
            out.add("off");
        } else if (args.length == 2 && "logs".equalsIgnoreCase(args[0])) {
            for (Player p : dev.acserver.acswitcher.Players.online()) out.add(p.getName());
        }
        return out;
    }
}