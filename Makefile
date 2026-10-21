# all_bind
#
#   make                 build the engine (./all_bind)
#   make install         install to $(DESTDIR)$(PREFIX) (PREFIX=/usr by default)
#   make uninstall       remove what `make install` put there
#   make dist            source tarball all_bind-VERSION.tar.gz
#   make pkg             build an Arch package locally with makepkg (no download)
#   make clean           remove build output
#
# Prefer `make pkg` + `sudo pacman -U packaging/all_bind-*.pkg.tar.zst` over
# `sudo make install`, so pacman can track and remove the files.

PREFIX  ?= /usr
DESTDIR ?=
CFLAGS  ?= -O2
CFLAGS  += -Wall -Wextra -pthread
LDFLAGS += -pthread

VERSION := $(shell sed -n 's/^pkgver=//p' packaging/PKGBUILD)
DISTNAME = all_bind-$(VERSION)

DIST_FILES = all_bind.c all_bind_gui.py Makefile README.md LICENSE .gitignore \
             packaging/PKGBUILD packaging/all_bind.install packaging/all_bind.desktop \
             packaging/all_bind@.service packaging/60-all_bind-uinput.rules \
             packaging/all_bind-modules.conf

BIN     = $(DESTDIR)$(PREFIX)/bin
SYSTEMD = $(DESTDIR)$(PREFIX)/lib/systemd/system
UDEV    = $(DESTDIR)$(PREFIX)/lib/udev/rules.d
MODLOAD = $(DESTDIR)$(PREFIX)/lib/modules-load.d
APPS    = $(DESTDIR)$(PREFIX)/share/applications
DOC     = $(DESTDIR)$(PREFIX)/share/doc/all_bind
LICDIR  = $(DESTDIR)$(PREFIX)/share/licenses/all_bind

.PHONY: all install uninstall dist pkg clean

all: all_bind

all_bind: all_bind.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS)

install: all_bind
	install -Dm755 all_bind                          $(BIN)/all_bind
	install -Dm755 all_bind_gui.py                   $(BIN)/all_bind-gui
	install -Dm644 packaging/all_bind@.service       $(SYSTEMD)/all_bind@.service
	install -Dm644 packaging/60-all_bind-uinput.rules $(UDEV)/60-all_bind-uinput.rules
	install -Dm644 packaging/all_bind-modules.conf   $(MODLOAD)/all_bind.conf
	install -Dm644 packaging/all_bind.desktop        $(APPS)/all_bind.desktop
	install -Dm644 README.md                         $(DOC)/README.md
	install -Dm644 LICENSE                           $(LICDIR)/LICENSE
	@if [ -z "$(DESTDIR)" ]; then \
	    echo; \
	    echo "Installed. Next steps:"; \
	    echo "  sudo systemctl daemon-reload && sudo udevadm control --reload"; \
	    echo "  sudo modprobe uinput             (or reboot)"; \
	    echo "  then either: sudo usermod -aG input \$$USER  (log out/in) and run all_bind at login,"; \
	    echo "           or: sudo systemctl enable --now all_bind@\$$USER"; \
	    echo "  set it up:   all_bind --gui   or   all_bind --cli help"; \
	fi

uninstall:
	rm -f $(BIN)/all_bind $(BIN)/all_bind-gui $(SYSTEMD)/all_bind@.service \
	      $(UDEV)/60-all_bind-uinput.rules $(MODLOAD)/all_bind.conf \
	      $(APPS)/all_bind.desktop $(DOC)/README.md $(LICDIR)/LICENSE
	-rmdir $(DOC) $(LICDIR) 2>/dev/null

dist: $(DISTNAME).tar.gz

$(DISTNAME).tar.gz: $(DIST_FILES)
	tar -czf $@ --transform 's,^,$(DISTNAME)/,' $(DIST_FILES)

# makepkg uses packaging/$(DISTNAME).tar.gz instead of downloading it.
pkg: dist
	cp $(DISTNAME).tar.gz packaging/
	cd packaging && makepkg -f --cleanbuild
	@echo
	@echo "Install with:  sudo pacman -U packaging/$$(cd packaging && ls -t all_bind-[0-9]*.pkg.tar.* | head -n1)"

clean:
	rm -f all_bind $(DISTNAME).tar.gz packaging/$(DISTNAME).tar.gz packaging/*.pkg.tar.*
	rm -rf packaging/src packaging/pkg
