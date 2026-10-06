package io.github.drbrainlesslol.forgestudio

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import java.io.File

/** Where Forge Studio runs and how to reach it. Empty token = read it from Forge Studio's own config folder. */
@Service(Service.Level.APP)
@State(name = "ForgeStudioSettings", storages = [Storage("forge-studio.xml")])
class ForgeSettings : PersistentStateComponent<ForgeSettings.Data> {
    class Data {
        var port: Int = 8765
        var token: String = ""
        var launcher: String = ""
        var startServer: Boolean = true
    }

    private var data = Data()
    override fun getState() = data
    override fun loadState(state: Data) { data = state }

    val port get() = data.port
    val baseUrl get() = "http://127.0.0.1:${data.port}"

    /** The app token: set by hand, or the one Forge Studio wrote to ~/.config/forge-studio/token. */
    fun token(): String = data.token.trim().ifEmpty { tokenFile().takeIf { it.isFile }?.readText()?.trim().orEmpty() }

    /** The `forge-studio` launcher: set by hand, or found on PATH / in the usual install places. */
    fun launcher(): String? {
        data.launcher.trim().takeIf { it.isNotEmpty() }?.let { return it }
        val home = System.getProperty("user.home")
        val path = (System.getenv("PATH") ?: "").split(File.pathSeparator)
        val candidates = path.map { File(it, "forge-studio") } +
            listOf(File(home, ".local/bin/forge-studio"), File("/usr/local/bin/forge-studio"), File("/usr/bin/forge-studio"),
                File(home, ".local/lib/forge-studio/forge-studio"), File("/opt/forge-studio/forge-studio"),
                File("/usr/lib/forge-studio/forge-studio"))
        return candidates.firstOrNull { it.isFile && it.canExecute() }?.path
    }

    companion object {
        fun get(): ForgeSettings = service()
        fun tokenFile() = File(System.getProperty("user.home"), ".config/forge-studio/token")
    }
}
