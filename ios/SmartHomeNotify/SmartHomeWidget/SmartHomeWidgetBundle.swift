//
//  SmartHomeWidgetBundle.swift
//  SmartHomeWidget
//
//  Created by chris priest on 3/30/26.
//

import WidgetKit
import SwiftUI

@main
struct SmartHomeWidgetBundle: WidgetBundle {
    var body: some Widget {
        SmartHomeWidget()
        GasPriceWidget()
        SmartHomeWidgetControl()
        SmartHomeWidgetLiveActivity()
    }
}
