# CareerPilot iOS branding

The checked-in App Store icon and launch artwork were generated with OpenAI image
generation in standard text-to-image and image-reference editing modes, then resized to
Apple's required asset dimensions. Both final PNGs are opaque and intentionally contain no
text or pre-rendered rounded corners.

## App icon

Generated in text-to-image mode with this prompt:

> Use case: logo-brand. Asset type: iOS App Store icon, final square raster artwork.
> Create a distinctive premium icon for CareerPilot, an AI career companion. Use an
> abstract upward flight path combined with a subtle compass/pilot motif to suggest guided
> career progress and grounded direction. Use polished minimal vector-like editorial brand
> artwork with gentle dimensional depth and crisp geometric forms, not photorealism. Center
> a bold symbol in a full-bleed 1:1 square with generous optical padding for Apple's rounded
> icon mask. The mood is calm, capable, trustworthy, and quietly optimistic. Use deep forest
> green `#143c31`, warm cream `#f4f0e8`, muted mint, and one restrained peach accent
> `#f1a36f`. Use an opaque background and strong small-size legibility. Do not include
> letters, words, numbers, people, resume/document clichés, an Apple logo, muddy gradients,
> a watermark, rounded corners, or a device mockup.

The final 1024 × 1024 icon is at
`App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png`.

## Launch artwork

Generated in image-reference editing mode from the approved icon with this prompt:

> Use case: logo-brand. Asset type: iOS launch-screen artwork, a 1:1 square source designed
> for aspect-fill on portrait and landscape devices. Recompose the supplied CareerPilot app
> icon into a calm launch screen. Preserve the same abstract compass and upward flight-path
> symbol and the same forest-green, cream, mint, and peach palette. Use a warm cream
> full-bleed background. Place a simplified version of the approved symbol exactly centered
> and no more than 32% of the canvas width, with extremely generous clear space so
> aspect-fill cropping never cuts it off. Keep the polished, minimal, vector-like artwork
> crisp and quiet. Use an opaque background. Do not add text, letters, words, numbers,
> people, devices, mockups, borders, rounded corners, watermarks, extra symbols, or alter the
> core motif. Keep all important artwork within the central 40% of the square.

The three 2732 × 2732 scale slots are in
`App/App/Assets.xcassets/Splash.imageset/`.

Do not regenerate these assets during a normal web or iOS build. If branding changes,
generate a new source, visually review it, resize it into these exact asset slots, confirm
that the files have no alpha channel, and validate the asset catalog in Xcode.
