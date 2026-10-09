sbx policy allow network --sandbox "%SBX_NAME%" "deb.debian.org:80,deb.debian.org:443,security.debian.org:80,security.debian.org:443"
sbx policy allow network --sandbox "%SBX_NAME%" "apt.postgresql.org:443,apt.postgresql.org:80"
sbx policy allow network --sandbox "%SBX_NAME%" "pypi.org:443,*.pypi.org:443,files.pythonhosted.org:443"
sbx policy allow network --sandbox "%SBX_NAME%" "mirrors.edge.kernel.org:443"
