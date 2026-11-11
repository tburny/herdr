{
  lib,
  stdenvNoCC,
  fetchurl,
}:

let
  pins = lib.importJSON ./pins.json;
  system = stdenvNoCC.hostPlatform.system;
  asset = pins.assets.${system} or (throw "herdr: no release binary for ${system}");
in
stdenvNoCC.mkDerivation {
  pname = "herdr";
  inherit (pins) version;

  # The release workflow prefills the store with this exact file before the
  # release is public, so the name must stay the asset name.
  src = fetchurl {
    url = "https://github.com/herdrdev/herdr/releases/download/v${pins.version}/${asset.name}";
    inherit (asset) hash;
  };

  dontUnpack = true;
  # Install the release asset byte for byte: no strip, patchelf or re-signing.
  dontFixup = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 $src $out/bin/herdr
    runHook postInstall
  '';

  meta = {
    description = "Terminal workspace manager for AI coding agents";
    homepage = "https://herdr.dev";
    license = lib.licenses.asl20;
    mainProgram = "herdr";
    platforms = builtins.attrNames pins.assets;
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
