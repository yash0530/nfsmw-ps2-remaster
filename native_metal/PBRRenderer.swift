import Foundation
import Metal
import MetalKit
import simd

struct FrameUniforms {
    var viewProjMatrix: simd_float4x4
    var invViewProjMatrix: simd_float4x4
    var modelMatrix: simd_float4x4
    var cameraPos: SIMD4<Float>
    var sunDir: SIMD4<Float>
    var sunColor: SIMD4<Float>
    var coronaColor: SIMD4<Float>
    var skyZenith: SIMD4<Float>
    var skyHorizon: SIMD4<Float>
    var skyGround: SIMD4<Float>
    var postParams: SIMD4<Float>
    var screenSize: SIMD4<Float>
}

struct MaterialUniforms {
    var baseColor: SIMD4<Float>
    var secondaryColor: SIMD4<Float>
    var pbrParams: SIMD4<Float>
    var emissive: SIMD4<Float>
    var flags: SIMD4<Float>
}

struct LightingPreset {
    let key: String
    let name: String
    let sunDirection: SIMD3<Float>
    let sunColor: SIMD3<Float>
    let sunIntensity: Float
    let coronaColor: SIMD3<Float>
    let skyZenith: SIMD3<Float>
    let skyHorizon: SIMD3<Float>
    let skyGround: SIMD3<Float>
    let iblIntensity: Float
    let wetness: Float
    let fogDensity: Float
    let exposure: Float
    let bloomThreshold: Float
    let bloomIntensity: Float
    let headlightsOn: Float
}

struct ScenePartEntry {
    let mesh: LoadedMesh
    let role: String
    let transparent: Bool
}

struct OrbitCamera {
    var target: SIMD3<Float> = SIMD3<Float>(0.0, 0.56, 0.12)
    var distance: Float = 5.35
    var azimuthDeg: Float = 38.0
    var elevationDeg: Float = 11.5
    var fovDeg: Float = 40.0

    let defaultTarget: SIMD3<Float>
    let defaultDistance: Float
    let defaultAzimuthDeg: Float
    let defaultElevationDeg: Float

    init(target: SIMD3<Float>, distance: Float, azimuthDeg: Float, elevationDeg: Float, fovDeg: Float) {
        self.target = target
        self.distance = distance
        self.azimuthDeg = azimuthDeg
        self.elevationDeg = elevationDeg
        self.fovDeg = fovDeg
        self.defaultTarget = target
        self.defaultDistance = distance
        self.defaultAzimuthDeg = azimuthDeg
        self.defaultElevationDeg = elevationDeg
    }

    mutating func reset() {
        target = defaultTarget
        distance = defaultDistance
        azimuthDeg = defaultAzimuthDeg
        elevationDeg = defaultElevationDeg
    }

    func eyePosition() -> SIMD3<Float> {
        let az = azimuthDeg * .pi / 180.0
        let el = elevationDeg * .pi / 180.0
        let x = target.x + distance * cos(el) * sin(az)
        let y = max(0.22, target.y + distance * sin(el))
        let z = target.z + distance * cos(el) * cos(az)
        return SIMD3<Float>(x, y, z)
    }

    func viewMatrix() -> simd_float4x4 {
        return Math3D.lookAt(eye: eyePosition(), target: target, up: SIMD3<Float>(0, 1, 0))
    }

    func projectionMatrix(aspect: Float) -> simd_float4x4 {
        return Math3D.perspective(fovYRadians: fovDeg * .pi / 180.0, aspect: aspect, nearZ: 0.08, farZ: 220.0)
    }
}

enum Math3D {
    static func lookAt(eye: SIMD3<Float>, target: SIMD3<Float>, up: SIMD3<Float>) -> simd_float4x4 {
        let f = simd_normalize(target - eye)
        let s = simd_normalize(simd_cross(f, up))
        let u = simd_cross(s, f)
        return simd_float4x4(
            SIMD4<Float>(s.x, u.x, -f.x, 0),
            SIMD4<Float>(s.y, u.y, -f.y, 0),
            SIMD4<Float>(s.z, u.z, -f.z, 0),
            SIMD4<Float>(-simd_dot(s, eye), -simd_dot(u, eye), simd_dot(f, eye), 1)
        )
    }

    static func perspective(fovYRadians: Float, aspect: Float, nearZ: Float, farZ: Float) -> simd_float4x4 {
        let yScale = 1.0 / tan(fovYRadians * 0.5)
        let xScale = yScale / max(aspect, 0.01)
        let zRange = farZ - nearZ
        return simd_float4x4(
            SIMD4<Float>(xScale, 0, 0, 0),
            SIMD4<Float>(0, yScale, 0, 0),
            SIMD4<Float>(0, 0, -farZ / zRange, -1),
            SIMD4<Float>(0, 0, -(farZ * nearZ) / zRange, 0)
        )
    }
}

final class PBRRenderer: NSObject, MTKViewDelegate {
    let device: MTLDevice
    let commandQueue: MTLCommandQueue

    private var skyPSO: MTLRenderPipelineState!
    private var groundPSO: MTLRenderPipelineState!
    private var opaquePSO: MTLRenderPipelineState!
    private var glassPSO: MTLRenderPipelineState!
    private var bloomThresholdPSO: MTLRenderPipelineState!
    private var bloomBlurPSO: MTLRenderPipelineState!
    private var compositePSO_sRGB: MTLRenderPipelineState!
    private var compositePSO_Linear: MTLRenderPipelineState!

    private var depthWriteState: MTLDepthStencilState!
    private var depthReadOnlyState: MTLDepthStencilState!
    private var noDepthState: MTLDepthStencilState!
    private var linearSampler: MTLSamplerState!
    private var repeatSampler: MTLSamplerState!

    private var hdrSceneTex: MTLTexture?
    private var depthTex: MTLTexture?
    private var bloomTexA: MTLTexture?
    private var bloomTexB: MTLTexture?
    private var targetWidth: Int = 0
    private var targetHeight: Int = 0

    private var asphaltTexture: MTLTexture!
    private var carbonTexture: MTLTexture!
    private var hasCarbonTexture: Bool = false

    private var groundMesh: LoadedMesh!
    private var opaqueParts: [ScenePartEntry] = []
    private var glassParts: [ScenePartEntry] = []
    private var materialMap: [String: MaterialUniforms] = [:]
    private var presets: [String: LightingPreset] = [:]

    var activePresetKey: String = "golden_hour"
    var liveryEnabled: Bool = true
    var camera: OrbitCamera = OrbitCamera(
        target: SIMD3<Float>(0, 0.56, 0.12),
        distance: 5.35,
        azimuthDeg: 38.0,
        elevationDeg: 11.5,
        fovDeg: 40.0
    )

    init(device: MTLDevice, sceneURL: URL, presetKey: String = "golden_hour", liveryEnabled: Bool = true) throws {
        self.device = device
        guard let queue = device.makeCommandQueue() else {
            throw NSError(domain: "PBRRenderer", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not create MTLCommandQueue"])
        }
        self.commandQueue = queue
        self.activePresetKey = presetKey
        self.liveryEnabled = liveryEnabled
        super.init()

        let library = try Self.loadMetalLibrary(device: device)
        try buildPipelines(library: library)
        buildStates()
        try loadScene(sceneURL: sceneURL)
        self.groundMesh = ObjLoader.createGroundPlane(device: device, commandQueue: commandQueue)
    }

    private static func loadMetalLibrary(device: MTLDevice) throws -> MTLLibrary {
        let exeDir = Bundle.main.executableURL?.deletingLastPathComponent()
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)

        // 1. Try precompiled .metallib if present
        let metallibCandidates: [URL?] = [
            exeDir?.appendingPathComponent("RockportMetalPBR.metallib"),
            cwd.appendingPathComponent("build/metal_wave3/RockportMetalPBR.metallib"),
            cwd.appendingPathComponent("RockportMetalPBR.metallib"),
        ]
        for cand in metallibCandidates.compactMap({ $0 }) {
            if FileManager.default.fileExists(atPath: cand.path),
               let lib = try? device.makeLibrary(URL: cand) {
                return lib
            }
        }

        // 2. Fallback: compile Shaders.metal directly on Apple Silicon Metal GPU
        let sourceCandidates: [URL?] = [
            exeDir?.appendingPathComponent("Shaders.metal"),
            exeDir?.deletingLastPathComponent().appendingPathComponent("native_metal/Shaders.metal"),
            exeDir?.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("native_metal/Shaders.metal"),
            cwd.appendingPathComponent("native_metal/Shaders.metal"),
            cwd.appendingPathComponent("Shaders.metal"),
        ]
        for cand in sourceCandidates.compactMap({ $0 }) {
            if FileManager.default.fileExists(atPath: cand.path) {
                let src = try String(contentsOf: cand, encoding: .utf8)
                let opts = MTLCompileOptions()
                return try device.makeLibrary(source: src, options: opts)
            }
        }
        throw NSError(
            domain: "PBRRenderer",
            code: 2,
            userInfo: [NSLocalizedDescriptionKey: "Could not locate RockportMetalPBR.metallib or Shaders.metal"]
        )
    }

    private func buildPipelines(library: MTLLibrary) throws {
        let vDesc = ObjLoader.vertexDescriptor()

        func makePSO(
            vertexFn: String,
            fragmentFn: String,
            colorFormat: MTLPixelFormat,
            depthFormat: MTLPixelFormat = .depth32Float,
            withVertexDesc: Bool = true,
            blending: Bool = false
        ) throws -> MTLRenderPipelineState {
            let desc = MTLRenderPipelineDescriptor()
            desc.vertexFunction = library.makeFunction(name: vertexFn)
            desc.fragmentFunction = library.makeFunction(name: fragmentFn)
            if withVertexDesc {
                desc.vertexDescriptor = vDesc
            }
            desc.colorAttachments[0].pixelFormat = colorFormat
            if blending {
                desc.colorAttachments[0].isBlendingEnabled = true
                desc.colorAttachments[0].rgbBlendOperation = .add
                desc.colorAttachments[0].alphaBlendOperation = .add
                desc.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
                desc.colorAttachments[0].destinationRGBBlendFactor = .oneMinusSourceAlpha
                desc.colorAttachments[0].sourceAlphaBlendFactor = .one
                desc.colorAttachments[0].destinationAlphaBlendFactor = .oneMinusSourceAlpha
            }
            desc.depthAttachmentPixelFormat = depthFormat
            return try device.makeRenderPipelineState(descriptor: desc)
        }

        skyPSO = try makePSO(vertexFn: "skyVertex", fragmentFn: "skyFragment", colorFormat: .rgba16Float, withVertexDesc: false)
        groundPSO = try makePSO(vertexFn: "groundVertex", fragmentFn: "groundFragment", colorFormat: .rgba16Float)
        opaquePSO = try makePSO(vertexFn: "pbrVertex", fragmentFn: "pbrFragment", colorFormat: .rgba16Float)
        glassPSO = try makePSO(vertexFn: "pbrVertex", fragmentFn: "pbrFragment", colorFormat: .rgba16Float, blending: true)
        bloomThresholdPSO = try makePSO(vertexFn: "fullscreenVertex", fragmentFn: "bloomThresholdFragment", colorFormat: .rgba16Float, depthFormat: .invalid, withVertexDesc: false)
        bloomBlurPSO = try makePSO(vertexFn: "fullscreenVertex", fragmentFn: "bloomBlurFragment", colorFormat: .rgba16Float, depthFormat: .invalid, withVertexDesc: false)
        compositePSO_sRGB = try makePSO(vertexFn: "fullscreenVertex", fragmentFn: "tonemapCompositeFragment", colorFormat: .bgra8Unorm, depthFormat: .invalid, withVertexDesc: false)
        compositePSO_Linear = try makePSO(vertexFn: "fullscreenVertex", fragmentFn: "tonemapCompositeFragment", colorFormat: .bgra8Unorm_srgb, depthFormat: .invalid, withVertexDesc: false)
    }

    private func buildStates() {
        let dw = MTLDepthStencilDescriptor()
        dw.depthCompareFunction = .lessEqual
        dw.isDepthWriteEnabled = true
        depthWriteState = device.makeDepthStencilState(descriptor: dw)

        let dro = MTLDepthStencilDescriptor()
        dro.depthCompareFunction = .lessEqual
        dro.isDepthWriteEnabled = false
        depthReadOnlyState = device.makeDepthStencilState(descriptor: dro)

        let nd = MTLDepthStencilDescriptor()
        nd.depthCompareFunction = .always
        nd.isDepthWriteEnabled = false
        noDepthState = device.makeDepthStencilState(descriptor: nd)

        let lin = MTLSamplerDescriptor()
        lin.minFilter = .linear
        lin.magFilter = .linear
        lin.mipFilter = .linear
        lin.sAddressMode = .clampToEdge
        lin.tAddressMode = .clampToEdge
        linearSampler = device.makeSamplerState(descriptor: lin)

        let rep = MTLSamplerDescriptor()
        rep.minFilter = .linear
        rep.magFilter = .linear
        rep.mipFilter = .linear
        rep.sAddressMode = .repeat
        rep.tAddressMode = .repeat
        rep.maxAnisotropy = 16
        repeatSampler = device.makeSamplerState(descriptor: rep)
    }

    private func ensureRenderTargets(width: Int, height: Int) {
        guard width > 0, height > 0, (width != targetWidth || height != targetHeight) else { return }
        targetWidth = width
        targetHeight = height

        let hdrDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba16Float,
            width: width,
            height: height,
            mipmapped: false
        )
        hdrDesc.usage = [.renderTarget, .shaderRead]
        hdrDesc.storageMode = .private
        hdrSceneTex = device.makeTexture(descriptor: hdrDesc)

        let depthDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .depth32Float,
            width: width,
            height: height,
            mipmapped: false
        )
        depthDesc.usage = [.renderTarget]
        depthDesc.storageMode = .private
        depthTex = device.makeTexture(descriptor: depthDesc)

        let bw = max(1, width / 2)
        let bh = max(1, height / 2)
        let bloomDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba16Float,
            width: bw,
            height: bh,
            mipmapped: false
        )
        bloomDesc.usage = [.renderTarget, .shaderRead]
        bloomDesc.storageMode = .private
        bloomTexA = device.makeTexture(descriptor: bloomDesc)
        bloomTexB = device.makeTexture(descriptor: bloomDesc)
    }

    private static func vec3(_ obj: Any?, fallback: SIMD3<Float>) -> SIMD3<Float> {
        guard let arr = obj as? [Any], arr.count >= 3 else { return fallback }
        let x = (arr[0] as? NSNumber)?.floatValue ?? fallback.x
        let y = (arr[1] as? NSNumber)?.floatValue ?? fallback.y
        let z = (arr[2] as? NSNumber)?.floatValue ?? fallback.z
        return SIMD3<Float>(x, y, z)
    }

    private static func flt(_ obj: Any?, fallback: Float) -> Float {
        return (obj as? NSNumber)?.floatValue ?? fallback
    }

    private static func materialKindId(_ kind: String) -> Float {
        switch kind {
        case "car_paint": return 0
        case "metal": return 1
        case "dielectric": return 2
        case "carbon_fiber": return 3
        case "rubber": return 4
        case "glass": return 5
        case "emissive_headlight": return 6
        case "emissive_taillight": return 7
        default: return 2
        }
    }

    private func loadScene(sceneURL: URL) throws {
        let data = try Data(contentsOf: sceneURL)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "PBRRenderer", code: 3, userInfo: [NSLocalizedDescriptionKey: "Invalid scene.json"])
        }
        let sceneDir = sceneURL.deletingLastPathComponent()

        // Camera
        if let cam = root["camera"] as? [String: Any] {
            camera = OrbitCamera(
                target: Self.vec3(cam["target"], fallback: SIMD3<Float>(0, 0.56, 0.12)),
                distance: Self.flt(cam["distance"], fallback: 5.35),
                azimuthDeg: Self.flt(cam["azimuth_deg"], fallback: 38.0),
                elevationDeg: Self.flt(cam["elevation_deg"], fallback: 11.5),
                fovDeg: Self.flt(cam["fov_deg"], fallback: 40.0)
            )
        }

        // Textures (CC0 baked wet asphalt + carbon fiber, searching repo root upwards from sceneDir)
        func resolveRepoAsset(_ rel: String) -> URL? {
            guard !rel.isEmpty else { return nil }
            var probe = sceneDir
            for _ in 0..<5 {
                let candidate = probe.appendingPathComponent(rel)
                if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
                probe = probe.deletingLastPathComponent()
            }
            let cwdCand = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(rel)
            return FileManager.default.fileExists(atPath: cwdCand.path) ? cwdCand : nil
        }

        let texDict = root["textures"] as? [String: Any] ?? [:]
        if let aspRel = texDict["asphalt_wet"] as? String,
           let aspURL = resolveRepoAsset(aspRel),
           let tex = TextureLoader.loadTexture(at: aspURL, device: device, commandQueue: commandQueue, maxDimension: 2048) {
            asphaltTexture = tex
        } else {
            asphaltTexture = TextureLoader.makeSolidTexture(device: device, rgba: (62, 64, 68, 255))!
        }

        if let cfRel = texDict["carbon_fiber"] as? String,
           let cfURL = resolveRepoAsset(cfRel),
           let tex = TextureLoader.loadTexture(at: cfURL, device: device, commandQueue: commandQueue, maxDimension: 1024) {
            carbonTexture = tex
            hasCarbonTexture = true
        } else {
            carbonTexture = TextureLoader.makeSolidTexture(device: device, rgba: (28, 30, 32, 255))!
            hasCarbonTexture = false
        }

        // Materials
        if let mats = root["materials"] as? [String: [String: Any]] {
            for (name, m) in mats {
                let base = Self.vec3(m["base_color"], fallback: SIMD3<Float>(0.7, 0.7, 0.7))
                let sec = Self.vec3(m["secondary_color"], fallback: base)
                let em = Self.vec3(m["emissive"], fallback: SIMD3<Float>(0, 0, 0))
                let kind = (m["kind"] as? String) ?? "dielectric"
                let alpha = Self.flt(m["alpha"], fallback: 1.0)
                let ior = Self.flt(m["ior"], fallback: 1.5)
                let metallic = Self.flt(m["metallic"], fallback: 0.0)
                let rough = Self.flt(m["roughness"], fallback: 0.4)
                let cc = Self.flt(m["clearcoat"], fallback: 0.0)
                let ccRough = Self.flt(m["clearcoat_roughness"], fallback: 0.05)
                let liv = (m["livery_enabled"] as? Bool) ?? false

                materialMap[name] = MaterialUniforms(
                    baseColor: SIMD4<Float>(base, alpha),
                    secondaryColor: SIMD4<Float>(sec, ior),
                    pbrParams: SIMD4<Float>(metallic, rough, cc, ccRough),
                    emissive: SIMD4<Float>(em, Self.materialKindId(kind)),
                    flags: SIMD4<Float>(liv ? 1.0 : 0.0, hasCarbonTexture ? 1.0 : 0.0, 1.0, 0.0)
                )
            }
        }

        // Lighting presets
        if let lps = root["lighting_presets"] as? [String: [String: Any]] {
            for (k, p) in lps {
                presets[k] = LightingPreset(
                    key: k,
                    name: (p["name"] as? String) ?? k,
                    sunDirection: simd_normalize(Self.vec3(p["sun_direction"], fallback: SIMD3<Float>(0.5, 0.3, 0.8))),
                    sunColor: Self.vec3(p["sun_color"], fallback: SIMD3<Float>(1.0, 0.9, 0.7)),
                    sunIntensity: Self.flt(p["sun_intensity"], fallback: 5.0),
                    coronaColor: Self.vec3(p["corona_color"], fallback: SIMD3<Float>(1.0, 0.65, 0.3)),
                    skyZenith: Self.vec3(p["sky_zenith"], fallback: SIMD3<Float>(0.12, 0.28, 0.62)),
                    skyHorizon: Self.vec3(p["sky_horizon"], fallback: SIMD3<Float>(0.92, 0.56, 0.30)),
                    skyGround: Self.vec3(p["sky_ground"], fallback: SIMD3<Float>(0.05, 0.05, 0.05)),
                    iblIntensity: Self.flt(p["ibl_intensity"], fallback: 1.3),
                    wetness: Self.flt(p["wetness"], fallback: 0.65),
                    fogDensity: Self.flt(p["fog_density"], fallback: 0.006),
                    exposure: Self.flt(p["exposure"], fallback: 1.1),
                    bloomThreshold: Self.flt(p["bloom_threshold"], fallback: 1.25),
                    bloomIntensity: Self.flt(p["bloom_intensity"], fallback: 0.45),
                    headlightsOn: Self.flt(p["headlights_on"], fallback: 1.0)
                )
            }
        }

        // Parts
        if let parts = root["parts"] as? [[String: Any]] {
            for p in parts {
                let visible = (p["visible"] as? Bool) ?? true
                guard visible, let rel = p["obj"] as? String else { continue }
                let name = (p["name"] as? String) ?? rel
                let role = (p["role"] as? String) ?? "body"
                let transparent = (p["transparent"] as? Bool) ?? false
                let objURL = sceneDir.appendingPathComponent(rel)
                let mesh = try ObjLoader.loadOBJ(at: objURL, device: device, name: name)
                let entry = ScenePartEntry(mesh: mesh, role: role, transparent: transparent)
                if transparent {
                    glassParts.append(entry)
                } else {
                    opaqueParts.append(entry)
                }
            }
        }
    }

    private func currentPreset() -> LightingPreset {
        if let p = presets[activePresetKey] { return p }
        if let p = presets["golden_hour"] { return p }
        return LightingPreset(
            key: "fallback",
            name: "Fallback",
            sunDirection: simd_normalize(SIMD3<Float>(0.35, 0.24, 0.90)),
            sunColor: SIMD3<Float>(1.0, 0.88, 0.68),
            sunIntensity: 5.5,
            coronaColor: SIMD3<Float>(1.0, 0.65, 0.3),
            skyZenith: SIMD3<Float>(0.11, 0.24, 0.52),
            skyHorizon: SIMD3<Float>(0.96, 0.56, 0.28),
            skyGround: SIMD3<Float>(0.05, 0.05, 0.05),
            iblIntensity: 1.35,
            wetness: 0.65,
            fogDensity: 0.008,
            exposure: 1.12,
            bloomThreshold: 1.25,
            bloomIntensity: 0.48,
            headlightsOn: 1.0
        )
    }

    private func makeFrameUniforms(width: Int, height: Int, encodeSRGB: Bool) -> FrameUniforms {
        let p = currentPreset()
        let aspect = Float(width) / Float(max(height, 1))
        let view = camera.viewMatrix()
        let proj = camera.projectionMatrix(aspect: aspect)
        let vp = proj * view
        let invVP = simd_inverse(vp)
        let eye = camera.eyePosition()

        return FrameUniforms(
            viewProjMatrix: vp,
            invViewProjMatrix: invVP,
            modelMatrix: matrix_identity_float4x4,
            cameraPos: SIMD4<Float>(eye, camera.fovDeg),
            sunDir: SIMD4<Float>(p.sunDirection, p.sunIntensity),
            sunColor: SIMD4<Float>(p.sunColor, p.exposure),
            coronaColor: SIMD4<Float>(p.coronaColor, p.iblIntensity),
            skyZenith: SIMD4<Float>(p.skyZenith, p.wetness),
            skyHorizon: SIMD4<Float>(p.skyHorizon, p.fogDensity),
            skyGround: SIMD4<Float>(p.skyGround, p.headlightsOn),
            postParams: SIMD4<Float>(p.bloomThreshold, p.bloomIntensity, liveryEnabled ? 1.0 : 0.0, encodeSRGB ? 1.0 : 0.0),
            screenSize: SIMD4<Float>(Float(width), Float(height), 1.0 / Float(width), 1.0 / Float(height))
        )
    }

    private func defaultMaterial() -> MaterialUniforms {
        return MaterialUniforms(
            baseColor: SIMD4<Float>(0.76, 0.79, 0.84, 1.0),
            secondaryColor: SIMD4<Float>(0.055, 0.21, 0.56, 1.5),
            pbrParams: SIMD4<Float>(0.85, 0.20, 1.0, 0.035),
            emissive: SIMD4<Float>(0, 0, 0, 0),
            flags: SIMD4<Float>(1, 0, 1, 0)
        )
    }

    func encodeFrame(
        commandBuffer: MTLCommandBuffer,
        outputTexture: MTLTexture,
        encodeSRGB: Bool
    ) {
        let w = outputTexture.width
        let h = outputTexture.height
        ensureRenderTargets(width: w, height: h)
        guard let hdrTex = hdrSceneTex,
              let depTex = depthTex,
              let bloomA = bloomTexA,
              let bloomB = bloomTexB else { return }

        var uniforms = makeFrameUniforms(width: w, height: h, encodeSRGB: encodeSRGB)

        // 1. HDR Scene Pass (Sky + Wet Asphalt Ground + Opaque Car + Transparent Glass)
        let sceneRP = MTLRenderPassDescriptor()
        sceneRP.colorAttachments[0].texture = hdrTex
        sceneRP.colorAttachments[0].loadAction = .clear
        sceneRP.colorAttachments[0].storeAction = .store
        sceneRP.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 1)
        sceneRP.depthAttachment.texture = depTex
        sceneRP.depthAttachment.loadAction = .clear
        sceneRP.depthAttachment.storeAction = .dontCare
        sceneRP.depthAttachment.clearDepth = 1.0

        if let enc = commandBuffer.makeRenderCommandEncoder(descriptor: sceneRP) {
            // Sky-dome
            enc.setRenderPipelineState(skyPSO)
            enc.setDepthStencilState(noDepthState)
            enc.setFragmentBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 0)
            enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)

            // Wet asphalt ground plane
            enc.setRenderPipelineState(groundPSO)
            enc.setDepthStencilState(depthWriteState)
            enc.setCullMode(.none)
            enc.setVertexBuffer(groundMesh.vertexBuffer, offset: 0, index: 0)
            enc.setVertexBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 1)
            enc.setFragmentBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 1)
            enc.setFragmentTexture(asphaltTexture, index: 0)
            enc.setFragmentSamplerState(repeatSampler, index: 0)
            enc.drawIndexedPrimitives(
                type: .triangle,
                indexCount: 6,
                indexType: .uint32,
                indexBuffer: groundMesh.indexBuffer,
                indexBufferOffset: 0
            )

            // Helper for drawing car parts
            func drawParts(_ list: [ScenePartEntry], pso: MTLRenderPipelineState, depthState: MTLDepthStencilState) {
                enc.setRenderPipelineState(pso)
                enc.setDepthStencilState(depthState)
                enc.setFrontFacing(.counterClockwise)
                enc.setCullMode(.none)
                enc.setVertexBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 1)
                enc.setFragmentBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 1)
                enc.setFragmentTexture(carbonTexture, index: 0)
                enc.setFragmentSamplerState(repeatSampler, index: 0)

                for part in list {
                    enc.setVertexBuffer(part.mesh.vertexBuffer, offset: 0, index: 0)
                    for sub in part.mesh.submeshes {
                        var mat = materialMap[sub.materialName] ?? defaultMaterial()
                        enc.setFragmentBytes(&mat, length: MemoryLayout<MaterialUniforms>.stride, index: 2)
                        enc.drawIndexedPrimitives(
                            type: .triangle,
                            indexCount: sub.indexCount,
                            indexType: .uint32,
                            indexBuffer: part.mesh.indexBuffer,
                            indexBufferOffset: sub.indexOffset * MemoryLayout<UInt32>.stride
                        )
                    }
                }
            }

            drawParts(opaqueParts, pso: opaquePSO, depthState: depthWriteState)
            drawParts(glassParts, pso: glassPSO, depthState: depthReadOnlyState)
            enc.endEncoding()
        }

        // 2. HDR Bloom Threshold (Full -> Half Res A)
        let threshRP = MTLRenderPassDescriptor()
        threshRP.colorAttachments[0].texture = bloomA
        threshRP.colorAttachments[0].loadAction = .dontCare
        threshRP.colorAttachments[0].storeAction = .store
        if let enc = commandBuffer.makeRenderCommandEncoder(descriptor: threshRP) {
            enc.setRenderPipelineState(bloomThresholdPSO)
            enc.setFragmentBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 0)
            enc.setFragmentTexture(hdrTex, index: 0)
            enc.setFragmentSamplerState(linearSampler, index: 0)
            enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
            enc.endEncoding()
        }

        // 3. Separable Gaussian Blur (2 passes H+V)
        let bw = Float(bloomA.width)
        let bh = Float(bloomA.height)
        for pass in 0..<2 {
            let radius: Float = pass == 0 ? 1.35 : 2.6
            var stepH = SIMD4<Float>(radius / bw, 0, 0, 0)
            let rpH = MTLRenderPassDescriptor()
            rpH.colorAttachments[0].texture = bloomB
            rpH.colorAttachments[0].loadAction = .dontCare
            rpH.colorAttachments[0].storeAction = .store
            if let enc = commandBuffer.makeRenderCommandEncoder(descriptor: rpH) {
                enc.setRenderPipelineState(bloomBlurPSO)
                enc.setFragmentBytes(&stepH, length: MemoryLayout<SIMD4<Float>>.stride, index: 0)
                enc.setFragmentTexture(bloomA, index: 0)
                enc.setFragmentSamplerState(linearSampler, index: 0)
                enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
                enc.endEncoding()
            }

            var stepV = SIMD4<Float>(0, radius / bh, 0, 0)
            let rpV = MTLRenderPassDescriptor()
            rpV.colorAttachments[0].texture = bloomA
            rpV.colorAttachments[0].loadAction = .dontCare
            rpV.colorAttachments[0].storeAction = .store
            if let enc = commandBuffer.makeRenderCommandEncoder(descriptor: rpV) {
                enc.setRenderPipelineState(bloomBlurPSO)
                enc.setFragmentBytes(&stepV, length: MemoryLayout<SIMD4<Float>>.stride, index: 0)
                enc.setFragmentTexture(bloomB, index: 0)
                enc.setFragmentSamplerState(linearSampler, index: 0)
                enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
                enc.endEncoding()
            }
        }

        // 4. ACES Filmic Composite -> Output Texture
        let compRP = MTLRenderPassDescriptor()
        compRP.colorAttachments[0].texture = outputTexture
        compRP.colorAttachments[0].loadAction = .dontCare
        compRP.colorAttachments[0].storeAction = .store
        if let enc = commandBuffer.makeRenderCommandEncoder(descriptor: compRP) {
            enc.setRenderPipelineState(encodeSRGB ? compositePSO_sRGB : compositePSO_Linear)
            enc.setFragmentBytes(&uniforms, length: MemoryLayout<FrameUniforms>.stride, index: 0)
            enc.setFragmentTexture(hdrTex, index: 0)
            enc.setFragmentTexture(bloomA, index: 1)
            enc.setFragmentSamplerState(linearSampler, index: 0)
            enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
            enc.endEncoding()
        }
    }

    func renderHeadless(width: Int, height: Int, outURL: URL) throws {
        let outDesc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .bgra8Unorm,
            width: width,
            height: height,
            mipmapped: false
        )
        outDesc.usage = [.renderTarget, .shaderRead]
        outDesc.storageMode = .shared
        guard let outTex = device.makeTexture(descriptor: outDesc),
              let cmdBuf = commandQueue.makeCommandBuffer() else {
            throw NSError(domain: "PBRRenderer", code: 20, userInfo: [NSLocalizedDescriptionKey: "Failed to create offscreen target"])
        }
        encodeFrame(commandBuffer: cmdBuf, outputTexture: outTex, encodeSRGB: true)
        cmdBuf.commit()
        cmdBuf.waitUntilCompleted()
        if let err = cmdBuf.error {
            throw err
        }
        try TextureLoader.writeBGRA8TextureToPNG(texture: outTex, outURL: outURL)
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        guard let drawable = view.currentDrawable,
              let cmdBuf = commandQueue.makeCommandBuffer() else { return }
        let encodeSRGB = (view.colorPixelFormat == .bgra8Unorm)
        encodeFrame(commandBuffer: cmdBuf, outputTexture: drawable.texture, encodeSRGB: encodeSRGB)
        cmdBuf.present(drawable)
        cmdBuf.commit()
    }
}
