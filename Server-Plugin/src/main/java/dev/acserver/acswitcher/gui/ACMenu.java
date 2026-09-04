package dev.acserver.acswitcher.gui;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import dev.acserver.acswitcher.ACRegistry;
import dev.acserver.acswitcher.player.PlayerStore;
import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.Material;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * The /ac inventory: slot 0 is Vanilla (always on, not toggleable), then one
 * item per installed anticheat, toggled by clicking.
 */
public final class ACMenu {

    public static final String TITLE = ChatColor.RED + "Anticheats";

    public static Inventory build(JavaPlugin plugin, PlayerStore store, Player viewer) {
        Inventory inv = Bukkit.createInventory(null, 9, TITLE);
        inv.setItem(0, item(Material.BARRIER, (short) 0,
                ChatColor.GREEN + "Vanilla",
                ChatColor.GRAY + "Baseline protection - always on.", "",
                ChatColor.GRAY + "Cannot be disabled."));
        ACRegistry registry = store.registry();
        int slot = 1;
        for (ACRegistry.AcDef d : registry.installed()) {
            boolean on = store.get(viewer).isEnabled(d.id);
            inv.setItem(slot++, item(Material.STAINED_GLASS_PANE, (short) (on ? 5 : 14),
                    (on ? ChatColor.GREEN : ChatColor.RED) + d.id,
                    ChatColor.GRAY + (on ? "Enabled for you." : "Disabled for you."), "",
                    ChatColor.GRAY + "Click to " + (on ? "disable" : "enable") + "."));
        }
        return inv;
    }

    private static ItemStack item(Material mat, short data, String display, String... lore) {
        ItemStack it = new ItemStack(mat, 1, data);
        ItemMeta meta = it.getItemMeta();
        meta.setDisplayName(display);
        List<String> l = new ArrayList<String>();
        Collections.addAll(l, lore);
        meta.setLore(l);
        it.setItemMeta(meta);
        return it;
    }

    public static final class ClickListener implements Listener {
        private final JavaPlugin plugin;
        private final PlayerStore store;

        public ClickListener(JavaPlugin plugin, PlayerStore store) {
            this.plugin = plugin;
            this.store = store;
        }

        @EventHandler
        public void onClick(InventoryClickEvent e) {
            if (e.getView() == null || !TITLE.equals(e.getView().getTitle())) return;
            e.setCancelled(true);
            if (!(e.getWhoClicked() instanceof Player)) return;
            Player p = (Player) e.getWhoClicked();
            ItemStack item = e.getCurrentItem();
            if (item == null || item.getType() == Material.AIR || !item.hasItemMeta()) return;
            String name = ChatColor.stripColor(item.getItemMeta().getDisplayName());
            if (name == null || name.isEmpty()) return;
            if (name.equalsIgnoreCase(ACRegistry.VANILLA)) {
                p.sendMessage(ChatColor.DARK_GRAY + "[" + ChatColor.GREEN + "ACSwitcher" + ChatColor.DARK_GRAY + "] "
                        + ChatColor.GRAY + "Vanilla is always on - it cannot be disabled.");
                return;
            }
            ACRegistry.AcDef d = store.registry().byName(name);
            if (d == null) return;
            boolean on = store.get(p).isEnabled(d.id);
            store.setEnabled(p, p.getUniqueId(), p.getName(), d, !on);
            p.sendMessage(ChatColor.DARK_GRAY + "[" + ChatColor.GREEN + "ACSwitcher" + ChatColor.DARK_GRAY + "] "
                    + ChatColor.GRAY + d.id + (on ? " disabled" : " enabled") + " for you.");
            p.openInventory(build(plugin, store, p));
        }
    }
}