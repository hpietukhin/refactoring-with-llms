import org.gradle.api.tasks.testing.Test

plugins {
    java
    id("com.gradleup.shadow") version "8.3.8"
}

repositories {
    mavenLocal()
    mavenCentral()
}

dependencies {
    implementation(libs.com.fasterxml.jackson.core.jackson.databind)
    implementation(libs.fr.inria.gforge.spoon.spoon.core)
}

group = "planning"
version = "0.1.0"
description = "spoon-ast-indexer"

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(17))
    }
}

tasks.withType<Test>().configureEach {
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
        showStandardStreams = false
    }
}

tasks.shadowJar {
    archiveFileName.set("spoon-ast-indexer.jar")
    mergeServiceFiles()
    exclude("META-INF/*.SF")
    exclude("META-INF/*.DSA")
    exclude("META-INF/*.RSA")
    manifest {
        attributes["Main-Class"] = "planning.ast.SpoonAstIndexer"
    }
}
