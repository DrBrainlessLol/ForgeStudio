package io.github.drbrainlesslol.forgestudio

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.options.Configurable
import com.intellij.openapi.ui.TextFieldWithBrowseButton
import com.intellij.openapi.fileChooser.FileChooserDescriptorFactory
import com.intellij.ui.components.JBCheckBox
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBPasswordField
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import javax.swing.JButton
import javax.swing.JComponent

/** Settings | Tools | Forge Studio */
class ForgeConfigurable : Configurable {
    private val port = JBTextField()
    private val token = JBPasswordField()
    private val launcher = TextFieldWithBrowseButton().apply {
        addBrowseFolderListener(null, FileChooserDescriptorFactory.createSingleFileDescriptor().withTitle("Forge Studio Launcher"))
    }
    private val start = JBCheckBox("Start Forge Studio when it isn't running")
    private val result = JBLabel(" ")

    override fun getDisplayName() = "Forge Studio"

    override fun createComponent(): JComponent {
        val test = JButton("Test Connection").apply {
            addActionListener {
                apply()
                result.text = "Checking…"
                ApplicationManager.getApplication().executeOnPooledThread {
                    val p = ForgeClient.ping()
                    ApplicationManager.getApplication().invokeLater {
                        result.text = if (p.ok) "Connected (${p.windows} window${if (p.windows == 1) "" else "s"} open)" else p.error
                    }
                }
            }
        }
        return FormBuilder.createFormBuilder()
            .addLabeledComponent("Port:", port)
            .addLabeledComponent("App token:", token)
            .addComponentToRightColumn(JBLabel("Leave empty to use ~/.config/forge-studio/token (Settings → Web access in Forge Studio).").apply { componentStyle = com.intellij.util.ui.UIUtil.ComponentStyle.SMALL })
            .addLabeledComponent("Launcher:", launcher)
            .addComponentToRightColumn(JBLabel("Leave empty to find the forge-studio command automatically.").apply { componentStyle = com.intellij.util.ui.UIUtil.ComponentStyle.SMALL })
            .addComponent(start)
            .addComponent(test)
            .addComponent(result)
            .addComponentFillVertically(javax.swing.JPanel(), 0)
            .panel
    }

    private val s get() = ForgeSettings.get().state

    override fun isModified() = port.text.trim() != s.port.toString() || String(token.password) != s.token ||
        launcher.text != s.launcher || start.isSelected != s.startServer

    override fun apply() {
        s.port = port.text.trim().toIntOrNull()?.takeIf { it in 1..65535 } ?: 8765
        s.token = String(token.password).trim()
        s.launcher = launcher.text.trim()
        s.startServer = start.isSelected
    }

    override fun reset() {
        port.text = s.port.toString()
        token.text = s.token
        launcher.text = s.launcher
        start.isSelected = s.startServer
    }
}
