allprojects {
    repositories {
        google()
        mavenCentral()
    }
}

val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}
// receive_sharing_intent 1.9.0 asks for "compileSdk 37", but the SDK installs that
// platform as "android-37.0", which this Android Gradle plugin can't resolve yet.
// Compile it against the same SDK as the app; it uses no Android 17 APIs.
// (Registered before evaluationDependsOn below, so it runs before AGP locks the DSL.)
subprojects {
    if (name == "receive_sharing_intent") {
        afterEvaluate {
            extensions.findByType(com.android.build.api.dsl.LibraryExtension::class.java)?.compileSdk = 36
        }
    }
}
subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
