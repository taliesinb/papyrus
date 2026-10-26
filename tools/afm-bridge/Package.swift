// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "afm-bridge",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "papyrus-afm",
            path: "Sources/papyrus-afm"
        )
    ]
)
