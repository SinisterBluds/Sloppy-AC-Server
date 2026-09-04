package dev.acserver.acswitcher;

import dev.acserver.acswitcher.player.PlayerStore;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

/** Loads state + applies bypass permissions on join; saves + cleans up on quit. */
public final class PlayerListener implements Listener {

    private final PlayerStore store;

    public PlayerListener(PlayerStore store) {
        this.store = store;
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent e) {
        Player p = e.getPlayer();
        store.noteName(p.getName());
        store.get(p.getUniqueId(), p.getName(), true);
        store.apply(p);
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent e) {
        Player p = e.getPlayer();
        PlayerStore.State s = store.get(p);
        if (s != null) store.save(s);
        store.clear(p);
    }
}