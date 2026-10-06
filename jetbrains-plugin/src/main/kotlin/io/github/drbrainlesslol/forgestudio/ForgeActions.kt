package io.github.drbrainlesslol.forgestudio

import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.CommonDataKeys
import com.intellij.openapi.application.ApplicationInfo
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project

/**
 * Editor / project-view actions: send the selection (or file) to Forge Studio with an instruction.
 * Forge selects the project and fills its message box so you can add details before sending.
 */
abstract class ForgeAction(private val action: String) : DumbAwareAction() {
    override fun getActionUpdateThread() = ActionUpdateThread.BGT

    override fun update(e: AnActionEvent) {
        e.presentation.isEnabledAndVisible = e.project?.basePath != null &&
            (action == "open" || action == "ask" || e.getData(CommonDataKeys.VIRTUAL_FILE)?.isDirectory == false)
    }

    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        val base = project.basePath ?: return
        val editor = e.getData(CommonDataKeys.EDITOR)
        val file = e.getData(CommonDataKeys.VIRTUAL_FILE)?.takeIf { !it.isDirectory }
        val payload = linkedMapOf<String, Any?>("action" to action, "project" to base,
            "ide" to ApplicationInfo.getInstance().fullApplicationName)
        if (action != "open" && file != null) {
            payload["file"] = file.path
            payload["language"] = (e.getData(CommonDataKeys.PSI_FILE)?.language?.id ?: file.extension)?.lowercase()
            val sel = editor?.selectionModel
            if (editor != null && sel != null && sel.hasSelection()) {
                val doc = editor.document
                payload["selection"] = sel.selectedText
                payload["startLine"] = doc.getLineNumber(sel.selectionStart) + 1
                payload["endLine"] = doc.getLineNumber(maxOf(sel.selectionStart, sel.selectionEnd - 1)) + 1
            }
        }
        ForgeToolWindowFactory.show(project)
        ApplicationManager.getApplication().executeOnPooledThread { deliver(project, payload) }
    }

    private fun deliver(project: Project, payload: Map<String, Any?>) {
        var ping = ForgeClient.ensureRunning()
        if (!ping.ok) return notify(project, ping.error ?: "Forge Studio isn't reachable", NotificationType.ERROR)
        // the tool window may still be loading the app: wait for a window to connect so it receives the prompt
        var waited = 0
        while (ping.windows == 0 && waited < 8000) {
            Thread.sleep(250); waited += 250
            ping = ForgeClient.ping()
        }
        ForgeClient.sendContext(payload).fold(
            { n -> if (n == 0) notify(project, "Forge Studio has no window open. Open the Forge Studio tool window or the app, then try again.", NotificationType.WARNING) },
            { err -> notify(project, err.message ?: "Couldn't reach Forge Studio", NotificationType.ERROR) },
        )
    }

    private fun notify(project: Project, text: String, type: NotificationType) {
        NotificationGroupManager.getInstance().getNotificationGroup("Forge Studio").createNotification(text, type).notify(project)
    }
}

class AskForgeAction : ForgeAction("ask")
class ExplainAction : ForgeAction("explain")
class FixAction : ForgeAction("fix")
class WriteTestsAction : ForgeAction("tests")
class OpenProjectAction : ForgeAction("open")
