A organização esta pessima

qualquer modulo com .whl pode ser desconsiderado

rastreando depoências
python -m pip install --dry-run --report report.json pacote
unzip -p pacote.whl '*.dist-info/METADATA'
readelf -d arquivo.so

Considerando que eu so vou olhar um pacote se a instalação dele falhar por dependencia, ou seja, se o pacote embutir as libs que ele precisa ele nao vai falhar. Considerando que eu tenho apenas o ambiente python rodando dentro de um ambiente confinado flapak e que nao tenho acesso a nada do sistema a não ser freedesktop inicialmente. O meu processo seria tentar instalar o pacote, se instalar normalmente apenas gera os dados que eu preciso (nome, buildsystem, build-commands, source, etc). Se o pacote tiver requisitos python, tenta fazer o mesmo para cada requisito, ou seja, instala e ve se da erro. Se um pacote falha por requisitos nao python ai sim eu vou examinar o pacote. 


data = {
    "extension_id": ext_id,
    "display_name": safe_name,
    "description": "...",
    "mount_path": mount,
    "build_backends": [],
    "pkgconfig_triggers": [],
    "library_triggers": [],
    "package_triggers": [],
    "provides_executables": sorted(result.executables),
    "provides_pkgconfig": sorted(result.pkgconfig),
    "provides_libraries": sorted(result.libraries),
    "env": {
        "PATH": f"{mount}/bin:$PATH"
    }
}
