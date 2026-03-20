PACKAGE  := mcp-bridge
VERSION  := 1.0.0
DEB_NAME := $(PACKAGE)_$(VERSION)_all.deb
BUILD_DIR := build/$(PACKAGE)_$(VERSION)

.PHONY: deb clean

deb:
	mkdir -p $(BUILD_DIR)/DEBIAN
	mkdir -p $(BUILD_DIR)/opt/mcp-bridge
	mkdir -p $(BUILD_DIR)/lib/systemd/system

	cp server.py $(BUILD_DIR)/opt/mcp-bridge/
	cp requirements.txt $(BUILD_DIR)/opt/mcp-bridge/
	cp mcp-bridge.service $(BUILD_DIR)/lib/systemd/system/

	# DEBIAN control files
	sed 's/^Source:.*//;s/^Build-Depends:.*//;s/^Standards-Version:.*//;s/^Rules-Requires-Root:.*//;s/^Section: net/Section: net\nVersion: $(VERSION)/' \
		debian/control | grep -v '^$$' | head -1 > /dev/null
	@echo "Package: $(PACKAGE)" > $(BUILD_DIR)/DEBIAN/control
	@echo "Version: $(VERSION)" >> $(BUILD_DIR)/DEBIAN/control
	@echo "Section: net" >> $(BUILD_DIR)/DEBIAN/control
	@echo "Priority: optional" >> $(BUILD_DIR)/DEBIAN/control
	@echo "Architecture: all" >> $(BUILD_DIR)/DEBIAN/control
	@echo "Depends: python3 (>= 3.10), python3-venv, dnsutils, iputils-ping, traceroute, whois" >> $(BUILD_DIR)/DEBIAN/control
	@echo "Maintainer: Warwick <warwick@localhost>" >> $(BUILD_DIR)/DEBIAN/control
	@echo "Description: MCP Bridge Server" >> $(BUILD_DIR)/DEBIAN/control
	@echo " HTTP 요청, DNS 조회, 네트워크 진단 기능을 제공하는" >> $(BUILD_DIR)/DEBIAN/control
	@echo " 경량 MCP(Model Context Protocol) 서버." >> $(BUILD_DIR)/DEBIAN/control

	cp debian/postinst $(BUILD_DIR)/DEBIAN/
	cp debian/prerm $(BUILD_DIR)/DEBIAN/
	cp debian/postrm $(BUILD_DIR)/DEBIAN/
	chmod 755 $(BUILD_DIR)/DEBIAN/postinst $(BUILD_DIR)/DEBIAN/prerm $(BUILD_DIR)/DEBIAN/postrm

	dpkg-deb --build $(BUILD_DIR) build/$(DEB_NAME)
	@echo ""
	@echo "✅ build/$(DEB_NAME) 생성 완료"

clean:
	rm -rf build/
