import CoreGraphics
import Foundation
import ImageIO
import Metal
import UniformTypeIdentifiers

enum TextureLoader {
    static func makeSolidTexture(device: MTLDevice, rgba: (UInt8, UInt8, UInt8, UInt8)) -> MTLTexture? {
        let desc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm_srgb,
            width: 1,
            height: 1,
            mipmapped: false
        )
        desc.usage = [.shaderRead]
        guard let tex = device.makeTexture(descriptor: desc) else { return nil }
        var px: [UInt8] = [rgba.0, rgba.1, rgba.2, rgba.3]
        tex.replace(
            region: MTLRegionMake2D(0, 0, 1, 1),
            mipmapLevel: 0,
            withBytes: &px,
            bytesPerRow: 4
        )
        return tex
    }

    static func loadTexture(
        at url: URL,
        device: MTLDevice,
        commandQueue: MTLCommandQueue,
        maxDimension: Int = 2048
    ) -> MTLTexture? {
        guard FileManager.default.fileExists(atPath: url.path),
              let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let cgImage = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            return nil
        }

        let srcW = cgImage.width
        let srcH = cgImage.height
        let scale = min(1.0, Double(maxDimension) / Double(max(srcW, srcH)))
        let width = max(1, Int((Double(srcW) * scale).rounded()))
        let height = max(1, Int((Double(srcH) * scale).rounded()))

        let bytesPerRow = width * 4
        var rawBytes = [UInt8](repeating: 0, count: bytesPerRow * height)
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
              let ctx = CGContext(
                  data: &rawBytes,
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bytesPerRow: bytesPerRow,
                  space: cs,
                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
              ) else {
            return nil
        }
        ctx.interpolationQuality = .high
        ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        let desc = MTLTextureDescriptor.texture2DDescriptor(
            pixelFormat: .rgba8Unorm_srgb,
            width: width,
            height: height,
            mipmapped: true
        )
        desc.usage = [.shaderRead]
        guard let tex = device.makeTexture(descriptor: desc) else { return nil }
        tex.replace(
            region: MTLRegionMake2D(0, 0, width, height),
            mipmapLevel: 0,
            withBytes: rawBytes,
            bytesPerRow: bytesPerRow
        )

        if let cmdBuf = commandQueue.makeCommandBuffer(),
           let blit = cmdBuf.makeBlitCommandEncoder() {
            blit.generateMipmaps(for: tex)
            blit.endEncoding()
            cmdBuf.commit()
            cmdBuf.waitUntilCompleted()
        }
        return tex
    }

    static func writeBGRA8TextureToPNG(texture: MTLTexture, outURL: URL) throws {
        let width = texture.width
        let height = texture.height
        let bytesPerRow = width * 4
        var bgra = [UInt8](repeating: 0, count: bytesPerRow * height)
        texture.getBytes(
            &bgra,
            bytesPerRow: bytesPerRow,
            from: MTLRegionMake2D(0, 0, width, height),
            mipmapLevel: 0
        )

        // Force opaque alpha = 255 for clean desktop preview/PNG
        var i = 3
        while i < bgra.count {
            bgra[i] = 255
            i += 4
        }

        guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
              let provider = CGDataProvider(data: Data(bgra) as CFData),
              let cgImage = CGImage(
                  width: width,
                  height: height,
                  bitsPerComponent: 8,
                  bitsPerPixel: 32,
                  bytesPerRow: bytesPerRow,
                  space: cs,
                  bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                  provider: provider,
                  decode: nil,
                  shouldInterpolate: false,
                  intent: .defaultIntent
              ) else {
            throw NSError(domain: "TextureLoader", code: 10, userInfo: [NSLocalizedDescriptionKey: "Failed to build CGImage"])
        }

        let dir = outURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        guard let dest = CGImageDestinationCreateWithURL(outURL as CFURL, UTType.png.identifier as CFString, 1, nil) else {
            throw NSError(domain: "TextureLoader", code: 11, userInfo: [NSLocalizedDescriptionKey: "Failed to open PNG destination \(outURL.path)"])
        }
        CGImageDestinationAddImage(dest, cgImage, nil)
        if !CGImageDestinationFinalize(dest) {
            throw NSError(domain: "TextureLoader", code: 12, userInfo: [NSLocalizedDescriptionKey: "Failed to finalize PNG at \(outURL.path)"])
        }
    }
}
