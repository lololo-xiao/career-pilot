# CareerPilot iOS branding

The active iOS artwork uses CareerPilot's V2 city, bird, and paper-plane direction. The
person is represented by a living bird: capable and already in flight. Pilot is represented
by the paper airplane traveling beside the person as a companion. The large-format project
thumbnail places that relationship above a quiet city; the small iOS mark removes the city
so the two subjects remain legible.

The source artwork was created with OpenAI's built-in image-generation tool and normalized
locally into opaque sRGB PNGs at Apple's required dimensions. The app icon and launch
artwork use only deep ink `#182421` and warm paper `#f4f0e8`.

The complete V2 source set and generation notes are in
`../../assets/branding/city-bird-plane-v2/`.

## App icon

The icon uses exactly one smooth, original soaring-bird silhouette and one smaller
folded-paper-airplane silhouette, aligned upward as companions. It has a full-bleed ink
background, a warm-paper mark, generous optical padding for Apple's mask, no text, and no
pre-rendered rounded corners.

The active 1024 × 1024 icon is at
`App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png`.

## Launch artwork

The launch screen inverts the icon: the paired ink mark is centered on a full-bleed warm
paper background. All meaningful artwork stays within the crop-safe center so aspect-fill
does not cut it off in portrait or landscape.

The three 2732 × 2732 scale slots are in
`App/App/Assets.xcassets/Splash.imageset/`.

## Regeneration prompt summary

> Create a minimal CareerPilot mark with exactly one smooth soaring-bird silhouette for the
> person and one smaller folded-paper-airplane silhouette for the AI companion. Align them
> upward together, keep their organic and geometric forms distinct, and use only deep ink
> and warm paper. No city at icon scale, text, compass, circle, gradient, texture, shadow,
> border, rounded corners, or watermark.

Do not regenerate these assets during a normal web or iOS build. If branding changes,
create a versioned source set, review it visually, resize it into these exact asset slots,
confirm that every final PNG is opaque, and validate the asset catalog in Xcode.
