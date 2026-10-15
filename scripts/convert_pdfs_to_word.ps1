param(
    [Parameter(Mandatory = $true)]
    [string[]]$InputPdf,

    [int]$Dpi = 180
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Escape-Xml([string]$Value) {
    [System.Security.SecurityElement]::Escape($Value)
}

function Get-PngSize([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = New-Object System.IO.BinaryReader($stream)
        $reader.ReadBytes(16) | Out-Null
        $widthBytes = $reader.ReadBytes(4)
        $heightBytes = $reader.ReadBytes(4)
        [array]::Reverse($widthBytes)
        [array]::Reverse($heightBytes)
        return @([BitConverter]::ToUInt32($widthBytes, 0), [BitConverter]::ToUInt32($heightBytes, 0))
    }
    finally {
        $stream.Dispose()
    }
}

function New-WordFromPdf([string]$PdfPath) {
    $source = Resolve-Path -LiteralPath $PdfPath
    if ([System.IO.Path]::GetExtension($source) -ne '.pdf') {
        throw "Expected a PDF file: $source"
    }

    $output = [System.IO.Path]::ChangeExtension($source, '.docx')
    $work = Join-Path ([System.IO.Path]::GetTempPath()) ("pdf_word_" + [guid]::NewGuid())
    $package = Join-Path $work 'package'
    $renderDir = Join-Path $work 'rendered'
    New-Item -ItemType Directory -Path $package, $renderDir | Out-Null

    try {
        $stagedPdf = Join-Path $work 'source.pdf'
        Copy-Item -LiteralPath $source -Destination $stagedPdf
        & pdftocairo -png -r $Dpi $stagedPdf (Join-Path $renderDir 'page')
        if ($LASTEXITCODE -ne 0) {
            throw "pdftocairo failed for $source"
        }

        $pages = Get-ChildItem -LiteralPath $renderDir -Filter 'page-*.png' |
            Sort-Object { [int]([regex]::Match($_.BaseName, '\\d+$').Value) }
        if ($pages.Count -eq 0) {
            throw "No rendered pages were created for $source"
        }

        # Use an existing Word package as the OOXML template. RAGFlow's DOCX
        # parser is less tolerant of a hand-built minimal package.
        $template = Get-ChildItem -LiteralPath ([System.IO.Path]::GetDirectoryName($source.Path)) -Filter '*.docx' |
            Where-Object { $_.Length -lt 200KB -and $_.FullName -ne $output } |
            Sort-Object Length |
            Select-Object -First 1 -ExpandProperty FullName
        if (-not $template) {
            throw 'A standard DOCX template was not found in the source directory.'
        }
        $stagedTemplate = Join-Path $work 'template.docx'
        Copy-Item -LiteralPath $template -Destination $stagedTemplate
        [System.IO.Compression.ZipFile]::ExtractToDirectory($stagedTemplate, $package)
        Remove-Item -LiteralPath (Join-Path $package 'word/media/*') -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path (Join-Path $package 'word/media') | Out-Null

        $body = New-Object System.Text.StringBuilder
        $relationships = New-Object System.Text.StringBuilder
        for ($index = 0; $index -lt $pages.Count; $index++) {
            $page = $pages[$index]
            $imageName = "image$($index + 1).png"
            Copy-Item -LiteralPath $page.FullName -Destination (Join-Path $package "word/media/$imageName")
            $size = Get-PngSize $page.FullName
            $maxWidth = 6200000
            $maxHeight = 8800000
            $scale = [Math]::Min($maxWidth / ($size[0] * 914400.0 / $Dpi), $maxHeight / ($size[1] * 914400.0 / $Dpi))
            $width = [int]($size[0] * 914400.0 / $Dpi * $scale)
            $height = [int]($size[1] * 914400.0 / $Dpi * $scale)
            $relationshipId = "rId$($index + 1)"
            [void]$relationships.AppendLine("  <Relationship Id=`"$relationshipId`" Type=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image`" Target=`"media/$imageName`"/>")
            $pageNumber = $index + 1
            [void]$body.AppendLine("<w:p><w:r><w:t>Page $pageNumber</w:t></w:r></w:p>")
            [void]$body.AppendLine("<w:p><w:r><w:drawing><wp:inline distT=`"0`" distB=`"0`" distL=`"0`" distR=`"0`" xmlns:wp=`"http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing`"><wp:extent cx=`"$width`" cy=`"$height`"/><wp:docPr id=`"$pageNumber`" name=`"Page $pageNumber`"/><a:graphic xmlns:a=`"http://schemas.openxmlformats.org/drawingml/2006/main`"><a:graphicData uri=`"http://schemas.openxmlformats.org/drawingml/2006/picture`"><pic:pic xmlns:pic=`"http://schemas.openxmlformats.org/drawingml/2006/picture`"><pic:nvPicPr><pic:cNvPr id=`"0`" name=`"$imageName`"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed=`"$relationshipId`" xmlns:r=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships`"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x=`"0`" y=`"0`"/><a:ext cx=`"$width`" cy=`"$height`"/></a:xfrm><a:prstGeom prst=`"rect`"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>")
            if ($index -lt $pages.Count - 1) {
                [void]$body.AppendLine('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
            }
        }

        @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>$body<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/></w:sectPr></w:body></w:document>
"@ | Set-Content -LiteralPath (Join-Path $package 'word/document.xml') -Encoding UTF8

        @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
$relationships</Relationships>
"@ | Set-Content -LiteralPath (Join-Path $package 'word/_rels/document.xml.rels') -Encoding UTF8

        if (Test-Path -LiteralPath $output) {
            Remove-Item -LiteralPath $output -Force
        }
        [System.IO.Compression.ZipFile]::CreateFromDirectory($package, $output)
        Write-Host "Created: $output"
    }
    finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

foreach ($pdf in $InputPdf) {
    New-WordFromPdf $pdf
}
