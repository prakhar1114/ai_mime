// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AIMimeIntents",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(
            name: "AIMimeIntents",
            targets: ["AIMimeIntents"]
        )
    ],
    targets: [
        .executableTarget(
            name: "AIMimeIntents",
            path: "Sources/AIMimeIntents"
        )
    ]
)
