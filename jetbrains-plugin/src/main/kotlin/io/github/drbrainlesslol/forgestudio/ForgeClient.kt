package io.github.drbrainlesslol.forgestudio

import com.intellij.openapi.diagnostic.thisLogger
import java.net.URI
import java.net.URLEncoder
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.time.Duration
import java.util.concurrent.TimeUnit

/** Talks to the local Forge Studio server: is it up, start it, and hand it code from the editor. */
object ForgeClient {
    private val http: HttpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build()

    data class Ping(val ok: Boolean, val windows: Int = 0, val error: String? = null)

    fun ping(): Ping {
        val s = ForgeSettings.get()
        return try {
            val req = HttpRequest.newBuilder(URI("${s.baseUrl}/api/ide/ping"))
                .header("X-Token", s.token()).timeout(Duration.ofSeconds(2)).GET().build()
            val r = http.send(req, HttpResponse.BodyHandlers.ofString())
            when (r.statusCode()) {
                200 -> Ping(true, Regex("\"windows\":\\s*(\\d+)").find(r.body())?.groupValues?.get(1)?.toInt() ?: 0)
                401 -> Ping(false, error = "Forge Studio rejected the token. Check it in Settings | Tools | Forge Studio.")
                else -> Ping(false, error = "Forge Studio answered ${r.statusCode()}")
            }
        } catch (e: Exception) {
            Ping(false, error = "Forge Studio isn't running on port ${s.port}")
        }
    }

    /** Start the server in the background with the `forge-studio --server-only` launcher, then wait for it. */
    fun ensureRunning(): Ping {
        ping().let { if (it.ok || it.error?.contains("token") == true) return it }
        val s = ForgeSettings.get()
        if (!s.state.startServer) return ping()
        val exe = s.launcher() ?: return Ping(false, error = "Forge Studio isn't running and the forge-studio launcher wasn't found. " +
            "Start Forge Studio, or set the launcher in Settings | Tools | Forge Studio.")
        try {
            val pb = ProcessBuilder(exe, "--server-only").redirectErrorStream(true)
            pb.environment()["FORGE_STUDIO_PORT"] = s.port.toString()
            pb.start().waitFor(15, TimeUnit.SECONDS)
        } catch (e: Exception) {
            thisLogger().warn("Couldn't start Forge Studio", e)
            return Ping(false, error = "Couldn't start Forge Studio: ${e.message}")
        }
        repeat(30) {
            ping().let { if (it.ok) return it }
            Thread.sleep(200)
        }
        return ping()
    }

    /** URL for the tool window / browser, optionally focused on a project folder. */
    fun appUrl(project: String?): String {
        val s = ForgeSettings.get()
        val p = project?.let { "&project=" + URLEncoder.encode(it, StandardCharsets.UTF_8) }.orEmpty()
        return "${s.baseUrl}/#token=${URLEncoder.encode(s.token(), StandardCharsets.UTF_8)}$p"
    }

    /** Send a file / selection to Forge Studio: it selects the project and fills the message box. */
    fun sendContext(payload: Map<String, Any?>): Result<Int> = runCatching {
        val s = ForgeSettings.get()
        val req = HttpRequest.newBuilder(URI("${s.baseUrl}/api/ide/context"))
            .header("X-Token", s.token()).header("Content-Type", "application/json")
            .timeout(Duration.ofSeconds(5)).POST(HttpRequest.BodyPublishers.ofString(json(payload))).build()
        val r = http.send(req, HttpResponse.BodyHandlers.ofString())
        if (r.statusCode() != 200) {
            val msg = Regex("\"error\":\\s*\"((?:[^\"\\\\]|\\\\.)*)\"").find(r.body())?.groupValues?.get(1)
            error(msg ?: "Forge Studio answered ${r.statusCode()}")
        }
        Regex("\"delivered\":\\s*(\\d+)").find(r.body())?.groupValues?.get(1)?.toInt() ?: 0
    }

    private fun json(v: Any?): String = when (v) {
        null -> "null"
        is String -> buildString {
            append('"')
            for (c in v) when {
                c == '"' -> append("\\\"")
                c == '\\' -> append("\\\\")
                c == '\n' -> append("\\n")
                c == '\r' -> append("\\r")
                c == '\t' -> append("\\t")
                c < ' ' -> append("\\u%04x".format(c.code))
                else -> append(c)
            }
            append('"')
        }
        is Number, is Boolean -> v.toString()
        is Map<*, *> -> v.entries.joinToString(",", "{", "}") { json(it.key.toString()) + ":" + json(it.value) }
        else -> json(v.toString())
    }
}
