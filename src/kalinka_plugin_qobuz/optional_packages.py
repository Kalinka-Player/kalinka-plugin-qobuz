"""Optional pip packages this plugin can install on demand.

The runtime of the server's shared text embedder, which playlist suggestions
need and the server does not install. Importing this module must not pull
the packages it lists: the deb build runs it as a script to emit
/opt/kalinka/allowed_packages/qobuz.json, and the install helper checks every
request against that allow-list.

Run as a script to print the manifest JSON:

    python src/kalinka_plugin_qobuz/optional_packages.py
"""

from __future__ import annotations

from kalinka_plugin_sdk import OptionalPackageSpec, print_optional_packages_manifest


PLUGIN_ID = "qobuz"


# Jamendo and localfiles offer the same keys, and a shared key must keep one spec.
OPTIONAL_PACKAGES: dict[str, OptionalPackageSpec] = {
    "onnxruntime": OptionalPackageSpec(
        pip_spec="onnxruntime>=1.17",
        description=(
            "ONNX inference runtime for the text embedder that matches a "
            "mood search to Qobuz's playlists."
        ),
        # Neither PyPI nor piwheels has a 32-bit ARM build.
        architectures=("x86_64", "aarch64"),
    ),
    "tokenizers": OptionalPackageSpec(
        pip_spec="tokenizers>=0.15",
        description=(
            "HuggingFace tokenizers used by the text embedder behind mood "
            "search."
        ),
    ),
}


if __name__ == "__main__":
    print_optional_packages_manifest(PLUGIN_ID, OPTIONAL_PACKAGES)
