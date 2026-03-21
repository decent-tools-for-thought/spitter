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
sha256sums=('c7b9abebefc2c213e5ffbb2f1d6dec3b1341f62a82045a249225a31c8b9e1bc5')

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
