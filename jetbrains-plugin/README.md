# Forge Studio for JetBrains IDEs

A plugin for IntelliJ-based IDEs (2024.3+: IntelliJ IDEA, Android Studio, PyCharm, WebStorm, GoLand, Rider, …).

- **Forge Studio tool window** (right side): the full app, focused on the open project.
- **Right-click → Forge Studio** in the editor or project view (also Tools → Forge Studio): Ask, Explain, Find and Fix
  Problems, Write Tests, Open Project. The selection (or file) and its file/line reference are put in Forge Studio's
  message box so you can add details before sending.
- **Settings | Tools | Forge Studio**: port (default 8765), app token (empty = read `~/.config/forge-studio/token`),
  the `forge-studio` launcher (found automatically) and whether to start Forge Studio when it isn't running.

The plugin only talks to `127.0.0.1`.

## Build

```sh
./gradlew buildPlugin            # builds against IntelliJ IDEA Community 2024.3 (downloaded once)
./gradlew buildPlugin -PlocalIde=/path/to/an/installed/ide   # faster: build against an IDE you have
```

Gradle needs Java 17+ to run; JDK 21 for compiling is downloaded automatically if it's missing.
Install `build/distributions/forge-studio-jetbrains-*.zip` with **Settings | Plugins | ⚙ | Install Plugin from Disk**.

## Give the agent the IDE's tools

Turn on the IDE's own MCP server (**Settings | Tools | MCP Server → Enable MCP Server**, IDE 2025.2+). Forge Studio
detects it and passes it to Claude Code in Agent and Plan modes, so the agent can use the IDE's refactorings,
inspections, symbol info and run configurations. See Settings → General → JetBrains IDE in Forge Studio.
