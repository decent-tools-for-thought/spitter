pkgname=spitter
pkgver=0.1.0
pkgrel=1
pkgdesc="Self-documenting Cartesia speech CLI with websocket sessions"
arch=('any')
url="https://github.com/decent-tools-for-thought/spitter"
license=('MIT')
depends=('python' 'ffmpeg' 'pipewire' 'libpulse')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
source=("$pkgname-$pkgver.tar.gz::https://github.com/decent-tools-for-thought/spitter/releases/download/v$pkgver/$pkgname-$pkgver.tar.gz")
sha256sums=('74b8a2b08d3ee96928a290038af05d1380763b416b16816e480e4b8e825e4b8e')

build() {
  cd "$srcdir/$pkgname-$pkgver"
  /usr/bin/python -m build --wheel --no-isolation
}

package() {
  cd "$srcdir/$pkgname-$pkgver"
  /usr/bin/python -m installer --destdir="$pkgdir" dist/*.whl

  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
