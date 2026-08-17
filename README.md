# AUTOMATIC1111 WebUI [miniPaint](https://github.com/viliusle/miniPaint) extension

## Installation

This extension is listed in the official extension index an can be installed easily:  
Extensions -> Available -> Click `Load from` -> Search for `miniPaint` and press `Install`

## About this extension

This extension provides a integrated version of the [miniPaint](https://github.com/viliusle/miniPaint) image editor.  
![preview](images/img1.png)
It is a simple image editing tool but still satisfies most needs when trying to edit images.  
It provides the ability to send images to Img2Img, Controlnet and Extras.![Send button](images/img2.png)  
Images can also be sent from txt2img, img2img and extras directly to the extension via the 'Send to miniPaint' Button.![Send to miniPaint](images/img3.png)  

## Compatibility

Works with AUTOMATIC1111 and the Forge family, including [Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic/tree/neo).

Forge Neo mixes component types - img2img, Inpaint and ControlNet inputs are `ForgeCanvas`, while
Extras is an ordinary `gr.Image`. The extension detects the type of each destination it writes to,
so it does not need to know which WebUI or Gradio version it is running under.

Sending an image waits for the destination to actually accept it before switching tabs, so on a
remote or slow connection the target tab does not appear until the image is committed - and a
transfer that fails says so in the console instead of leaving you on a tab that looks ready. For
`ForgeCanvas` the image is written to the hidden textbox that Forge submits, not just to the
canvas you can see, because those are not the same value.

img2img generates from whichever of its sub-tabs (img2img / Inpaint / ...) the WebUI *itself*
has recorded, and it only learns about a sub-tab change through a request to the server. Sending
an image therefore also waits for that to be acknowledged, otherwise pressing Generate straight
after a send can render from a different slot - the classic "my image is right there and it was
ignored".

Every send is checked against the value the WebUI will actually submit: miniPaint decodes it back
and compares it, pixel for pixel, with the image it exported. A send that does not match is
retried, and if it still does not match you get a toast in the editor saying it could not send
and why, rather than a destination that merely looks right. Successful sends say so too, and the
console line records whether the value is byte-identical or was re-encoded by the host.

Every send is written to a log file inside this extension's folder:

```
extensions/a1111-mini-paint-NEO/logs/send-log.txt
```

Each entry records the transfer step by step with timings - what was exported, what the
destination held before and after each attempt, when the canvas displayed it, what the WebUI
ended up holding, and why an attempt was rejected:

```
[2026-08-17 20:24:56] #img2img_image -> sent: 512x512, byte-identical
    +    0ms  exported image - 512x512, 800000 bytes, hash abc
    +  340ms  canvas displays the image
    +  900ms  verified - byte-identical, on attempt 1
```

That file is the thing to attach to a bug report. It is written by the extension itself, so it
keeps working when what failed is the WebUI's own round trip, and it rotates once it passes 1 MB.
The same information is in the browser console, and `a1111minipaint.sendLog()` typed into the
console prints the last ten transfers if you would rather look there.

If a transfer does not work, open the browser console and run:

```js
a1111minipaint.debugReport()
```

It prints the Gradio version, whether ForgeCanvas is present, which destination IDs were found,
and how many ControlNet units are mounted - please include that output in bug reports.

## Issues, Code ownership and contribution

This extension is mostly code slammed together from other extensions all being free to use. If you want to grab parts of it, go ahead.  
If you find a bug, just report it over the issues section on github. Because i am still in school, feel free to fix the issues yourself and create a pull request.

## Modifying for yourself

If you want to customize certain things which are inside the miniPaint iframe, go into the miniPaint directory and run `npm run dev`  or `npm run build` and then reload your ui.
