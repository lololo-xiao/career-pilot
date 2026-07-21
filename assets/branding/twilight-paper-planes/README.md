# CareerPilot twilight paper-plane concept

This alternate visual direction translates the mood of an evening-sky paper plane into
original CareerPilot artwork. Two planes share the same upward journey: the warm-cream
plane represents the user and the mint plane represents Pilot, a close companion rather
than a replacement for the person.

The artwork is not based on or copied from song, album, or promotional artwork.

## Palette

- Twilight forest: `#143c31`
- Warm paper: `#f4f0e8`
- Companion mint: `#a7d6c0`
- Dusk peach: `#f1a36f`

## Deliverables

- `careerpilot-thumbnail-3x2-1536x1024.png`: project thumbnail, 3:2, opaque RGB,
  under the 5 MB upload limit shown in the project form.
- `careerpilot-app-icon-twilight-1024.png`: iOS App Store icon master, 1024 square,
  opaque RGB, with no text or pre-rendered rounded corners.
- `careerpilot-launch-twilight-2732.png`: launch artwork, 2732 square, opaque RGB,
  with the identifying symbol contained in the crop-safe center.

These are review-ready alternates. They intentionally do not overwrite the currently
active icon and splash assets. After approval, the icon can replace
`frontend/ios/App/App/Assets.xcassets/AppIcon.appiconset/AppIcon-512@2x.png`, and the
launch artwork can replace each of the three files in
`frontend/ios/App/App/Assets.xcassets/Splash.imageset/`.

App Store screenshots should be captured from the final tested build so they accurately
represent the shipped interface and features; generated UI artwork should not be used as
a substitute for those screenshots.

## Generation method and prompts

The source artwork was created with the built-in OpenAI image-generation tool, then resized
and normalized locally to the production dimensions above.

### Thumbnail

> Create an original, premium, minimalist 3:2 editorial illustration inspired only by the
> emotional idea of a paper airplane crossing an evening sky. Show two folded-paper planes
> traveling together along one gentle upward arc: a warm-cream person plane and a smaller
> mint AI-companion plane. Use a full-bleed forest-green and dusk-indigo sky fading into a
> restrained peach afterglow, subtle paper grain, strong silhouettes, and generous breathing
> room. Convey quiet optimism and “you are not alone.” No text, logos, people, compass,
> devices, office objects, real aircraft, album-art imitation, borders, or watermark.

### App icon

> Distill the thumbnail direction into a 1:1 iOS icon: one compact symbol made from two
> folded-paper planes traveling together on a single rising curve. Use a full-bleed opaque
> twilight-forest background, warm cream, muted mint, and one restrained peach accent.
> Center a bold, simple silhouette with generous optical padding for Apple's mask and strong
> 32 px readability. No text, compass, pre-rendered rounded corners, border, mockup, or
> watermark.

### Launch artwork

> Recompose the same paired-plane icon as a quiet square launch screen on a full-bleed warm
> cream background with barely visible peach and mint twilight glow. Center the simplified
> symbol at no more than 28% of the canvas and keep all important artwork inside the central
> 36% for aspect-fill crop safety. No text, extra symbols, devices, borders, rounded corners,
> or watermark.
