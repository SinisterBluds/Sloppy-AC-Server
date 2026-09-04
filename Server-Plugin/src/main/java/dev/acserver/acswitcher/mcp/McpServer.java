package dev.acserver.acswitcher.mcp;

import java.io.IOException;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.Callable;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import dev.acserver.acswitcher.ACRegistry;
import dev.acserver.acswitcher.flags.FlagLog;
import dev.acserver.acswitcher.player.PlayerStore;
import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Minimal MCP (streamable-HTTP style) server: JSON-RPC 2.0 over HTTP POST on
 * /mcp, Bearer-token auth, tools initialize / notifications/initialized / ping /
 * tools/list / tools/call. Runs on JDK-8 stdlib only (no MCP SDK - that needs
 * Java 21). Register in any MCP client as:
 *   { "mcpServers": { "acswitcher": { "url": "http://127.0.0.1:8765/mcp",
 *     "headers": { "Authorization": "Bearer <token>" } } } }
 */
public final class McpServer {

    public static final String PROTOCOL = "2025-06-18";

    public interface ToolFn {
        JsonObject run(JsonObject args) throws Exception;
    }

    public static final class Tool {
        public final String name;
        public final String description;
        public final JsonObject inputSchema;
        public final ToolFn fn;

        Tool(String name, String description, JsonObject inputSchema, ToolFn fn) {
            this.name = name;
            this.description = description;
            this.inputSchema = inputSchema;
            this.fn = fn;
        }
    }

    private final JavaPlugin plugin;
    private final ACRegistry registry;
    private final FlagLog flagLog;
    private final PlayerStore store;
    private final List<Tool> tools = new ArrayList<Tool>();
    private String token = "";
    private HttpServer server = null;

    public McpServer(JavaPlugin plugin, ACRegistry registry, FlagLog flagLog, PlayerStore store) {
        this.plugin = plugin;
        this.registry = registry;
        this.flagLog = flagLog;
        this.store = store;
        buildTools();
    }

    public boolean isRunning() {
        return server != null;
    }

    public String address() {
        return plugin.getConfig().getString("mcp.bind-address", "127.0.0.1") + ":"
                + plugin.getConfig().getInt("mcp.port", 8765)
                + plugin.getConfig().getString("mcp.path", "/mcp");
    }

    // ---------------------------------------------------------------- tools

    private void buildTools() {
        tools.add(new Tool("list_anticheats",
                "List all configured anticheats with install status. Vanilla is always on.",
                schema(),
                args -> {
                    JsonObject out = new JsonObject();
                    out.addProperty("vanilla_always_on", true);
                    JsonArray arr = new JsonArray();
                    for (ACRegistry.AcDef d : registry.all()) {
                        JsonObject o = new JsonObject();
                        o.addProperty("id", d.id);
                        o.addProperty("plugin", d.pluginName);
                        o.addProperty("installed", d.installed());
                        o.addProperty("bypass_permission", d.bypassPermission);
                        arr.add(o);
                    }
                    out.add("anticheats", arr);
                    return out;
                }));

        tools.add(new Tool("list_players",
                "List online players with their enabled anticheats and flag count.",
                schema(),
                args -> {
                    JsonObject out = new JsonObject();
                    JsonArray arr = new JsonArray();
                    for (Player p : dev.acserver.acswitcher.Players.online()) {
                        JsonObject o = playerJson(p.getName(), true);
                        arr.add(o);
                    }
                    out.add("players", arr);
                    return out;
                }));

        tools.add(new Tool("get_player_anticheat",
                "Get a player's anticheat state (which ACs are on). Player may be offline.",
                schema(stringProp("player", true, "Player name")),
                args -> {
                    String name = args.has("player") ? args.get("player").getAsString() : "";
                    Player online = Bukkit.getPlayerExact(name);
                    if (online != null) return playerJson(name, true);
                    PlayerStore.State s = store.offline(name);
                    if (s == null) {
                        JsonObject o = new JsonObject();
                        o.addProperty("player", name);
                        o.addProperty("known", false);
                        return o;
                    }
                    return stateJson(name, false, s);
                }));

        tools.add(new Tool("set_player_anticheat",
                "Enable or disable an anticheat for a player (online or offline; creates a record for unknown players).",
                schema(stringProp("player", true, "Player name"),
                        stringProp("anticheat", true, "Anticheat id, e.g. Intave / Spartan (see list_anticheats)"),
                        boolProp("enabled", true, "true = AC on, false = AC off (player gets the bypass permission)")),
                args -> {
                    String name = args.get("player").getAsString();
                    String acName = args.get("anticheat").getAsString();
                    boolean enabled = args.get("enabled").getAsBoolean();
                    ACRegistry.AcDef ac = registry.byName(acName);
                    if (ac == null) throw new IllegalArgumentException("Unknown anticheat '" + acName
                            + "'. Known: " + registry.names());
                    Player online = Bukkit.getPlayerExact(name);
                    PlayerStore.State st;
                    if (online != null) st = store.get(online);
                    else {
                        st = store.offline(name);
                        if (st == null) {
                            // pre-provision: deterministic UUID so the same name maps to the same record
                            st = store.get(java.util.UUID.nameUUIDFromBytes(
                                    name.toLowerCase(Locale.ROOT).getBytes(StandardCharsets.UTF_8)), name, true);
                        }
                    }
                    store.setEnabled(online, st.uuid, st.name, ac, enabled);
                    return stateJson(st.name, online != null, st);
                }));

        tools.add(new Tool("get_flag_logs",
                "Recent anticheat flags for a player.",
                schema(stringProp("player", true, "Player name"),
                        intProp("limit", false, "Max entries (default 20)")),
                args -> {
                    String name = args.get("player").getAsString();
                    int limit = args.has("limit") ? args.get("limit").getAsInt() : 20;
                    List<FlagLog.Flag> flags = flagLog.recent(name, Math.max(1, Math.min(500, limit)));
                    JsonObject out = new JsonObject();
                    out.addProperty("player", name);
                    out.addProperty("count", flags.size());
                    JsonArray arr = new JsonArray();
                    for (FlagLog.Flag f : flags) {
                        JsonObject o = new JsonObject();
                        o.addProperty("time", f.time);
                        o.addProperty("anticheat", f.ac);
                        o.addProperty("detail", f.detail);
                        arr.add(o);
                    }
                    out.add("flags", arr);
                    return out;
                }));

        tools.add(new Tool("server_command",
                "Execute a command as the server console.",
                schema(stringProp("command", true, "Command without leading slash")),
                args -> {
                    String cmd = args.get("command").getAsString();
                    final boolean ok = callSync(new Callable<Boolean>() {
                        @Override
                        public Boolean call() {
                            return Bukkit.dispatchCommand(Bukkit.getConsoleSender(), cmd.trim());
                        }
                    });
                    JsonObject o = new JsonObject();
                    o.addProperty("success", ok == Boolean.TRUE);
                    o.addProperty("command", cmd);
                    return o;
                }));

        tools.add(new Tool("get_server_info",
                "Server basics: version, motd, player counts.",
                schema(),
                args -> {
                    JsonObject o = new JsonObject();
                    o.addProperty("version", Bukkit.getServer().getVersion());
                    o.addProperty("bukkit_version", Bukkit.getBukkitVersion());
                    o.addProperty("motd", Bukkit.getServer().getMotd());
                    o.addProperty("online_players", dev.acserver.acswitcher.Players.onlineCount());
                    o.addProperty("max_players", Bukkit.getMaxPlayers());
                    return o;
                }));
    }

    private JsonObject playerJson(String name, boolean online) {
        Player p = online ? Bukkit.getPlayerExact(name) : null;
        PlayerStore.State s = p != null ? store.get(p) : store.offline(name);
        return stateJson(name, online, s);
    }

    private JsonObject stateJson(String name, boolean online, PlayerStore.State s) {
        JsonObject o = new JsonObject();
        o.addProperty("player", name);
        o.addProperty("online", online);
        o.addProperty("vanilla_always_on", true);
        JsonArray enabled = new JsonArray();
        JsonArray disabled = new JsonArray();
        if (s != null) {
            for (ACRegistry.AcDef d : registry.all()) {
                if (s.isEnabled(d.id)) enabled.add(d.id);
                else disabled.add(d.id);
            }
            o.addProperty("flag_count", flagLog.count(name));
        }
        o.add("enabled", enabled);
        o.add("disabled", disabled);
        return o;
    }

    /** Run Bukkit work on the main thread, blocking until done. */
    private <T> T callSync(Callable<T> c) {
        try {
            Future<T> f = Bukkit.getScheduler().callSyncMethod(plugin, c);
            return f.get(10, TimeUnit.SECONDS);
        } catch (Exception e) {
            throw new RuntimeException("Main-thread call failed: " + e.getMessage());
        }
    }

    // ------------------------------------------------------- JSON-RPC layer

    public void start() {
        token = plugin.getConfig().getString("mcp.token", "");
        if (!plugin.getConfig().getBoolean("mcp.enabled", true)) {
            plugin.getLogger().info("MCP API disabled by config.");
            return;
        }
        if (token.isEmpty()) {
            plugin.getLogger().warning("MCP API disabled: no mcp.token configured.");
            return;
        }
        try {
            server = HttpServer.create(new InetSocketAddress(
                    plugin.getConfig().getString("mcp.bind-address", "127.0.0.1"),
                    plugin.getConfig().getInt("mcp.port", 8765)), 0);
            server.createContext(plugin.getConfig().getString("mcp.path", "/mcp"), this::handle);
            server.start();
            plugin.getLogger().info("MCP API listening on " + address());
        } catch (IOException e) {
            plugin.getLogger().warning("MCP API could not bind " + address() + ": " + e.getMessage());
            server = null;
        }
    }

    public void stop() {
        if (server != null) server.stop(0);
        server = null;
    }

    private void handle(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            ex.sendResponseHeaders(405, -1);
            ex.close();
            return;
        }
        if (!authorized(ex)) {
            byte[] body = "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32001,\"message\":\"Unauthorized\"},\"id\":null}"
                    .getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().set("Content-Type", "application/json");
            ex.sendResponseHeaders(401, body.length);
            ex.getResponseBody().write(body);
            ex.close();
            return;
        }

        JsonObject req;
        try {
            StringBuilder sb = new StringBuilder();
            java.io.Reader r = new InputStreamReader(ex.getRequestBody(), StandardCharsets.UTF_8);
            char[] buf = new char[4096];
            int n;
            while ((n = r.read(buf)) > 0) sb.append(buf, 0, n);
            req = new JsonParser().parse(sb.toString()).getAsJsonObject();
        } catch (Exception e) {
            respond(ex, 400, error(com.google.gson.JsonNull.INSTANCE, -32700, "Parse error"));
            return;
        }

        String method = req.has("method") ? req.get("method").getAsString() : null;
        JsonElement idEl = req.has("id") ? req.get("id") : null;
        boolean notification = idEl == null || idEl.isJsonNull();

        JsonObject payload = dispatch(method, req);
        if (notification) {
            ex.sendResponseHeaders(204, -1);
            ex.close();
            return;
        }
        JsonObject resp = new JsonObject();
        resp.addProperty("jsonrpc", "2.0");
        if (payload.has("error")) resp.add("error", payload.get("error"));
        else resp.add("result", payload.get("result"));
        resp.add("id", idEl);
        byte[] body = resp.toString().getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(200, body.length);
        ex.getResponseBody().write(body);
        ex.close();
    }

    /** Returns a JsonObject with either "result" or "error" inside (never both). */
    private JsonObject dispatch(String method, JsonObject req) {
        if ("initialize".equals(method)) {
            JsonObject result = new JsonObject();
            JsonElement clientProtocol = req.has("params")
                    && req.get("params").isJsonObject()
                    && req.getAsJsonObject("params").has("protocolVersion")
                    ? req.getAsJsonObject("params").get("protocolVersion") : null;
            result.addProperty("protocolVersion", clientProtocol != null && clientProtocol.isJsonPrimitive()
                    ? clientProtocol.getAsString() : PROTOCOL);
            JsonObject caps = new JsonObject();
            caps.add("tools", new JsonObject());
            result.add("capabilities", caps);
            JsonObject info = new JsonObject();
            info.addProperty("name", "acswitcher");
            info.addProperty("version", plugin.getDescription().getVersion());
            result.add("serverInfo", info);
            return ok(result);
        }
        if ("ping".equals(method)) return ok(new JsonObject());
        if ("tools/list".equals(method)) {
            JsonObject result = new JsonObject();
            JsonArray arr = new JsonArray();
            for (Tool t : tools) {
                JsonObject o = new JsonObject();
                o.addProperty("name", t.name);
                o.addProperty("description", t.description);
                o.add("inputSchema", t.inputSchema);
                arr.add(o);
            }
            result.add("tools", arr);
            return ok(result);
        }
        if ("tools/call".equals(method)) {
            JsonObject params = req.has("params") && req.get("params").isJsonObject()
                    ? req.getAsJsonObject("params") : new JsonObject();
            String name = params.has("name") ? params.get("name").getAsString() : "";
            for (Tool t : tools) {
                if (!t.name.equals(name)) continue;
                JsonObject arguments = params.has("arguments") && params.get("arguments").isJsonObject()
                        ? params.getAsJsonObject("arguments") : new JsonObject();
                try {
                    JsonObject data = t.fn.run(arguments);
                    JsonObject result = new JsonObject();
                    JsonArray content = new JsonArray();
                    JsonObject text = new JsonObject();
                    text.addProperty("type", "text");
                    text.addProperty("text", data.toString());
                    content.add(text);
                    result.add("content", content);
                    result.addProperty("isError", false);
                    return ok(result);
                } catch (Throwable e) {
                    JsonObject result = new JsonObject();
                    JsonArray content = new JsonArray();
                    JsonObject text = new JsonObject();
                    text.addProperty("type", "text");
                    text.addProperty("text", e.getMessage() == null ? e.toString() : e.getMessage());
                    content.add(text);
                    result.add("content", content);
                    result.addProperty("isError", true);
                    return ok(result);
                }
            }
            return error(com.google.gson.JsonNull.INSTANCE, -32602, "Unknown tool: " + name);
        }
        // any unknown method (incl. notifications/*): JSON-RPC method not found
        return error(com.google.gson.JsonNull.INSTANCE, -32601, "Method not found: " + method);
    }

    private JsonObject ok(JsonObject result) {
        JsonObject o = new JsonObject();
        o.add("result", result);
        return o;
    }

    private JsonObject error(JsonElement id, int code, String message) {
        JsonObject o = new JsonObject();
        JsonObject err = new JsonObject();
        err.addProperty("code", code);
        err.addProperty("message", message);
        o.add("error", err);
        o.add("id", id);
        return o;
    }

    private boolean authorized(HttpExchange ex) {
        String auth = ex.getRequestHeaders().getFirst("Authorization");
        if (auth != null && auth.startsWith("Bearer ")) return timingSafeEquals(auth.substring(7), token);
        String t = ex.getRequestHeaders().getFirst("X-MCP-Token");
        return t != null && timingSafeEquals(t, token);
    }

    private static boolean timingSafeEquals(String a, String b) {
        if (a == null || b == null || a.length() != b.length()) return false;
        int diff = 0;
        for (int i = 0; i < a.length(); i++) diff |= a.charAt(i) ^ b.charAt(i);
        return diff == 0;
    }

    private void respond(HttpExchange ex, int code, JsonObject payload) throws IOException {
        byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(code, body.length);
        ex.getResponseBody().write(body);
        ex.close();
    }

    // --------------------------------------------------- JSON schema helpers

    private static JsonObject schema(JsonObject... props) {
        JsonObject schema = new JsonObject();
        schema.addProperty("type", "object");
        JsonObject properties = new JsonObject();
        JsonArray required = new JsonArray();
        for (JsonObject p : props) {
            properties.add(p.get("name").getAsString(), p);
            if (p.has("required") && p.get("required").getAsBoolean()) required.add(p.get("name").getAsString());
        }
        schema.add("properties", properties);
        if (required.size() > 0) schema.add("required", required);
        return schema;
    }

    private static JsonObject stringProp(String name, boolean required, String description) {
        JsonObject o = new JsonObject();
        o.addProperty("name", name);
        o.addProperty("type", "string");
        o.addProperty("description", description);
        o.addProperty("required", required);
        return o;
    }

    private static JsonObject boolProp(String name, boolean required, String description) {
        JsonObject o = new JsonObject();
        o.addProperty("name", name);
        o.addProperty("type", "boolean");
        o.addProperty("description", description);
        o.addProperty("required", required);
        return o;
    }

    private static JsonObject intProp(String name, boolean required, String description) {
        JsonObject o = new JsonObject();
        o.addProperty("name", name);
        o.addProperty("type", "integer");
        o.addProperty("description", description);
        o.addProperty("required", required);
        return o;
    }
}