# Changelog

Todas as alterações relevantes deste projeto serão documentadas aqui. O projeto segue [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-06-29

Primeira versão pública.

### Adicionado

- GUI Windows em tema escuro, drag-and-drop, fila, progresso, cancelamento e pré-visualização.
- CLI com `doctor`, `analyze`, `process` e modos automáticos ou manuais.
- Deteção integral de progressivo, PAL 2:2, NTSC 3:2, híbrido e entrelaçado real.
- Pipeline `hybrid50`: VFM para 25p e QTGMC apenas nos segmentos PAL 50i confirmados.
- QTGMC com source matching conservador, suporte TFF/BFF e saída 50p/59,94p.
- H.264, HEVC 10-bit e FFV1, com áudio, legendas, anexos e capítulos copiados.
- Auto-crop conservador opcional, desligado por defeito.
- Saídas atómicas, SHA-256, manifestos versionados e validação integral de FPS, DAR/SAR e combing.
- Ambiente portátil VapourSynth R76 + vs-jetpack e executáveis PyInstaller.

[0.1.0]: https://github.com/Pessegos/dvd-fieldfix/releases/tag/v0.1.0
