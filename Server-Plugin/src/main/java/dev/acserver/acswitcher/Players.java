package dev.acserver.acswitcher;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.List;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;

/**
 * PandaSpigot backports BOTH getOnlinePlayers() overloads (Player[] and
 * Collection) into one classfile, so javac's choice is unreliable. This
 * helper works whichever one the running server actually returns.
 */
public final class Players {

    private Players() {
    }

    public static List<Player> online() {
        Object o = Bukkit.getOnlinePlayers();
        if (o instanceof Player[]) return new ArrayList<Player>(Arrays.asList((Player[]) o));
        return new ArrayList<Player>((Collection<? extends Player>) o);
    }

    public static int onlineCount() {
        return online().size();
    }
}