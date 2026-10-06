plugins {
    id("org.jetbrains.kotlin.jvm") version "2.3.0"
    id("org.jetbrains.intellij.platform") version "2.19.0"
}

group = "io.github.drbrainlesslol"
version = providers.gradleProperty("pluginVersion").get()

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

val localIde = providers.gradleProperty("localIde").orNull?.takeIf { it.isNotBlank() }

dependencies {
    intellijPlatform {
        if (localIde != null) local(localIde) else intellijIdeaCommunity("2024.3.6")
    }
}

kotlin { jvmToolchain(21) }

intellijPlatform {
    buildSearchableOptions = false
    pluginConfiguration {
        version = project.version.toString()
        ideaVersion {
            sinceBuild = "243"
            untilBuild = provider { null }
        }
    }
}
