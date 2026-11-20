"""Download the models the helper needs, so the first start is not slow."""
import speak

print("Kokoro voice...")
speak.model()
print("Parakeet speech-to-text...")
from parakeet_mlx import from_pretrained
from_pretrained("mlx-community/parakeet-tdt-0.6b-v2")
print("Models ready.")
