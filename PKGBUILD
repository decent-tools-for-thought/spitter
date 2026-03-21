pkgname=spitter
pkgver=0.1.0
pkgrel=1
pkgdesc="Self-documenting Cartesia speech CLI with websocket sessions"
arch=('any')
url="https://docs.cartesia.ai/get-started/overview"
license=('MIT')
depends=('python' 'ffmpeg' 'pipewire' 'libpulse')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
source=()
sha256sums=()

_pkgsrcdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

build() {
  cd "$_pkgsrcdir"
  /usr/bin/python -m build --wheel --no-isolation
}

package() {
  cd "$_pkgsrcdir"
  /usr/bin/python -m installer --destdir="$pkgdir" dist/*.whl

  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
