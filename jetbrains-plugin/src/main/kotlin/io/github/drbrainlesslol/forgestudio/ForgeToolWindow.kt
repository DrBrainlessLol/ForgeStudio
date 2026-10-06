package io.github.drbrainlesslol.forgestudio

import com.intellij.icons.AllIcons
import com.intellij.ide.BrowserUtil
import com.intellij.openapi.Disposable
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.ui.components.JBLabel
import com.intellij.ui.content.ContentFactory
import com.intellij.ui.jcef.JBCefApp
import com.intellij.ui.jcef.JBCefBrowser
import com.intellij.util.ui.JBUI
import java.awt.BorderLayout
import java.awt.FlowLayout
import javax.swing.JButton
import javax.swing.JPanel
import javax.swing.SwingConstants

const val TOOL_WINDOW_ID = "Forge Studio"

/** Forge Studio inside the IDE: the full app in an embedded browser, focused on this project. */
class ForgeToolWindowFactory : ToolWindowFactory, com.intellij.openapi.project.DumbAware {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = ForgePanel(project)
        val content = ContentFactory.getInstance().createContent(panel, "", false)
        content.setDisposer(panel)
        toolWindow.contentManager.addContent(content)
        toolWindow.setTitleActions(listOf(
            object : DumbAwareAction("Reload", "Reload Forge Studio", AllIcons.Actions.Refresh) {
                override fun actionPerformed(e: AnActionEvent) = panel.load()
            },
            object : DumbAwareAction("Open in Browser", "Open Forge Studio in your browser", AllIcons.General.Web) {
                override fun actionPerformed(e: AnActionEvent) = BrowserUtil.browse(ForgeClient.appUrl(project.basePath))
            },
        ))
    }

    companion object {
        fun show(project: Project, then: () -> Unit = {}) {
            ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID)?.activate(then, true) ?: then()
        }
    }
}

class ForgePanel(private val project: Project) : JPanel(BorderLayout()), Disposable {
    private val browser: JBCefBrowser? = if (JBCefApp.isSupported()) JBCefBrowser().also { Disposer.register(this, it) } else null
    private val status = JBLabel("Starting Forge Studio…", SwingConstants.CENTER)

    init {
        load()
    }

    fun load() {
        removeAll()
        add(status, BorderLayout.CENTER)
        status.text = "Starting Forge Studio…"
        revalidate(); repaint()
        ApplicationManager.getApplication().executeOnPooledThread {
            val ping = ForgeClient.ensureRunning()
            ApplicationManager.getApplication().invokeLater {
                if (project.isDisposed) return@invokeLater
                removeAll()
                when {
                    !ping.ok -> add(message(ping.error ?: "Forge Studio isn't reachable"), BorderLayout.CENTER)
                    browser == null -> add(message("This IDE runtime has no embedded browser (JCEF). Open Forge Studio in your browser instead."), BorderLayout.CENTER)
                    else -> {
                        browser.loadURL(ForgeClient.appUrl(project.basePath))
                        add(browser.component, BorderLayout.CENTER)
                    }
                }
                revalidate(); repaint()
            }
        }
    }

    private fun message(text: String) = JPanel(BorderLayout()).apply {
        border = JBUI.Borders.empty(16)
        add(JBLabel("<html><div style='text-align:center'>${text.replace("&", "&amp;").replace("<", "&lt;")}</div></html>", SwingConstants.CENTER), BorderLayout.CENTER)
        add(JPanel(FlowLayout(FlowLayout.CENTER)).apply {
            add(JButton("Retry").apply { addActionListener { load() } })
            add(JButton("Open in Browser").apply { addActionListener { BrowserUtil.browse(ForgeClient.appUrl(project.basePath)) } })
            add(JButton("Settings").apply {
                addActionListener {
                    com.intellij.openapi.options.ShowSettingsUtil.getInstance().showSettingsDialog(project, ForgeConfigurable::class.java)
                }
            })
        }, BorderLayout.SOUTH)
    }

    override fun dispose() {}
}
