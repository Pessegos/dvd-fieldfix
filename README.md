# DVD FieldFix

DVD FieldFix analisa MKVs provenientes de DVD e aplica o tratamento menos destrutivo possível:

- vídeo progressivo é copiado byte a byte;
- PAL 2:2 ou NTSC 3:2 recuperável usa `fieldmatch` e deinterlace condicional apenas nos frames residuais isolados;
- PAL híbrido usa VFM para o corpo 25p, duplica esses frames sem interpolação e usa QTGMC apenas nos segmentos 50i, produzindo 50p;
- vídeo realmente entrelaçado usa VapourSynth/QTGMC a 50p ou 59,94p;
- NTSC híbrido ou resultados contraditórios param para revisão em vez de arriscar uma cadência errada;
- a GUI usa tema escuro e disponibiliza auto-crop conservador, desligado por defeito.

Os originais nunca são substituídos. Por defeito, as saídas ficam numa subpasta `_fixed`, são escritas primeiro como `.partial.mkv` e só recebem o nome final após validação integral.

## Instalação para desenvolvimento

Requisitos: Windows, Python 3.10+ e FFmpeg/FFprobe no `PATH`.

```powershell
python -m pip install -e ".[gui,dev]"
dvd-fieldfix doctor
```

Para instalar VapourSynth R76 + QTGMC num Python 3.12 portátil e isolado:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_qtgmc.ps1
dvd-fieldfix doctor
```

O setup valida o SHA-256 do bootstrap oficial e guarda o ambiente em `.runtime/vapoursynth-portable`. Não modifica o Python do sistema.

## Utilização

Abrir a GUI:

```powershell
dvd-fieldfix gui
```

Analisar uma pasta:

```powershell
dvd-fieldfix analyze "C:\videos" --report relatorio.json
```

Processar automaticamente para H.264:

```powershell
dvd-fieldfix process "C:\videos" --codec h264
```

Outros exemplos:

```powershell
dvd-fieldfix process episodio.mkv --codec hevc10 --mode fieldmatch
dvd-fieldfix process episodio.mkv --codec hevc10 --mode hybrid50
dvd-fieldfix process episodio.mkv --codec ffv1 --mode qtgmc
dvd-fieldfix process episodio.mkv --crop 8:0:8:0 --denoise light
dvd-fieldfix process episodio.mkv --auto-crop
```

O auto-crop mede sete zonas distribuídas pelo episódio e remove apenas margens que todas as amostras consideram exteriores à imagem. Um crop manual `L:T:R:B` tem sempre prioridade.

Perfis de vídeo:

- `h264`: x264 CRF 16, preset veryslow, 8-bit;
- `hevc10`: x265 CRF 18, preset veryslow, 10-bit;
- `ffv1`: FFV1 level 3 lossless.

Áudio, legendas, capítulos, anexos, idiomas e disposições são copiados sem recodificação.

## Pipeline temporal

O residual depois de field matching é medido em janelas de um segundo. Janelas com atividade 50i estável são unidas, recebem 0,5 s de margem e ficam registadas no relatório. No modo `hybrid50`:

- VFM recupera os frames 25p com a mesma ordem de campo usada pelo QTGMC;
- `Interleave` duplica cada frame 25p exatamente, sem criar movimento artificial;
- QTGMC com source matching conservador substitui os segmentos 50i e frames ainda marcados como combed;
- a saída é CFR 50p progressiva e mantém a duração, o áudio e o DAR da origem.

O programa analisa integralmente cada ficheiro; nenhuma lista de episódios está codificada no código. O `pipeline_version` faz com que saídas antigas nunca sejam reutilizadas como se tivessem sido produzidas pelo algoritmo atual.

## Saídas e manifestos

Cada MKV concluído recebe um manifesto adjacente `ficheiro.mkv.dvd-fieldfix.json` com SHA-256 da origem/saída, versão do pipeline, configuração, análise e validação. A validação confirma descodificação integral, streams, duração, frame rate, progressividade, combing residual e DAR/SAR. Uma saída já validada com a mesma origem, versão e configuração é ignorada; colisões diferentes ficam bloqueadas.

## Build dos executáveis

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

São criados uma aplicação gráfica `DVD-FieldFix.exe` e um executável de consola `DVD-FieldFix-CLI.exe` em `dist/DVD-FieldFix`.
